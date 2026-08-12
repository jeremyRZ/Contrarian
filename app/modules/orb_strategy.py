"""Minute-bar Opening Range Breakout strategy for Hong Kong equities.

The engine is deliberately broker-independent.  A signal is confirmed at a
bar close and filled no earlier than the next bar open, avoiding look-ahead.
Prices are adjusted against the HK tick table and quantities are rounded down
to board lots by the caller-provided ``lot_size``.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import time
from math import floor
from typing import Iterable, Optional

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class OrbParams:
    opening_minutes: int = 15
    buffer_bps: float = 5.0
    confirm_bars: int = 2
    min_range_bps: float = 20.0
    max_range_bps: float = 250.0
    atr_multiplier: float = 1.0
    range_stop_ratio: float = 0.35
    min_stop_bps: float = 20.0
    reward_risk: float = 3.0
    max_hold_bars: int = 120
    last_entry_time: str = "14:30"
    force_exit_time: str = "15:55"
    risk_per_trade: float = 0.005
    max_position_pct: float = 0.15
    max_spread_bps: float = 15.0
    max_entry_slippage_bps: float = 15.0
    max_chase_band_ratio: float = 0.50
    min_net_reward_risk: float = 2.3
    allow_long: bool = True
    allow_short: bool = False
    # Conservative all-in estimate, per side. Override with observed fills.
    fee_bps_per_side: float = 12.0
    slippage_bps_per_side: float = 5.0
    require_above_vwap: bool = False
    failed_breakout_exit: bool = False
    failed_breakout_bars: int = 2


def _time(value: str) -> time:
    return time.fromisoformat(value)


def hk_tick(price: float) -> float:
    """SEHK equity price spread table (common board-lot securities)."""
    if price < 0.25:
        return 0.001
    if price < 0.5:
        return 0.005
    if price < 10:
        return 0.01
    if price < 20:
        return 0.02
    if price < 100:
        return 0.05
    if price < 200:
        return 0.1
    if price < 500:
        return 0.2
    if price < 1000:
        return 0.5
    if price < 2000:
        return 1.0
    if price < 5000:
        return 2.0
    return 5.0


def _true_range(frame: pd.DataFrame) -> pd.Series:
    prev = frame["close"].shift(1)
    return pd.concat([
        frame["high"] - frame["low"],
        (frame["high"] - prev).abs(),
        (frame["low"] - prev).abs(),
    ], axis=1).max(axis=1)


def _prepare(frame: pd.DataFrame) -> pd.DataFrame:
    required = {"time_key", "open", "high", "low", "close"}
    missing = required.difference(frame.columns)
    if missing:
        raise ValueError("missing columns: " + ", ".join(sorted(missing)))
    out = frame.copy()
    out["time_key"] = pd.to_datetime(out["time_key"])
    out = out.sort_values("time_key").drop_duplicates("time_key", keep="last")
    for col in ("open", "high", "low", "close", "volume", "turnover"):
        if col in out:
            out[col] = pd.to_numeric(out[col], errors="coerce")
    return out.dropna(subset=["open", "high", "low", "close"])


def prepare_bars(frame: pd.DataFrame) -> pd.DataFrame:
    """Public one-time normalisation helper for repeated research runs."""
    return _prepare(frame)


def _empty(reason: str, trade_date=None) -> dict:
    return {"traded": False, "skip_reason": reason, "trade_date": trade_date}


def run_day(frame: pd.DataFrame, params: OrbParams = OrbParams(), *,
            equity: float = 1_000_000.0, lot_size: int = 100,
            spread_bps: float = 0.0, shortable: bool = False,
            prepared: bool = False) -> dict:
    """Run one trading day. Stop wins ties when a 1-minute bar hits both exits."""
    df = frame if prepared else _prepare(frame)
    if df.empty:
        return _empty("no_bars")
    dates = df["time_key"].dt.date.unique()
    if len(dates) != 1:
        raise ValueError("run_day expects exactly one trading date")
    trade_date = str(dates[0])
    continuous = df[((df.time_key.dt.time >= time(9, 30)) &
                     (df.time_key.dt.time < time(12, 0))) |
                    ((df.time_key.dt.time >= time(13, 0)) &
                     (df.time_key.dt.time <= _time(params.force_exit_time)))]
    if len(continuous) < params.opening_minutes + params.confirm_bars + 1:
        return _empty("insufficient_bars", trade_date)
    opening = continuous.iloc[:params.opening_minutes]
    expected_last = pd.Timestamp.combine(dates[0], time(9, 30)) + pd.Timedelta(
        minutes=params.opening_minutes - 1)
    if opening.iloc[0].time_key.time() != time(9, 30) or opening.iloc[-1].time_key < expected_last:
        return _empty("incomplete_opening_window", trade_date)

    open_px = float(opening.iloc[0].open)
    range_hi, range_lo = float(opening.high.max()), float(opening.low.min())
    range_width = range_hi - range_lo
    range_bps = range_width / open_px * 10_000
    if range_bps < params.min_range_bps:
        return _empty("range_too_small", trade_date)
    if range_bps > params.max_range_bps:
        return _empty("range_too_large", trade_date)
    if spread_bps > params.max_spread_bps:
        return _empty("spread_too_wide", trade_date)

    atr = float(_true_range(opening).mean())
    band = max(atr * params.atr_multiplier,
               range_width * params.range_stop_ratio,
               open_px * params.min_stop_bps / 10_000,
               hk_tick(open_px) * 3)
    upper = range_hi * (1 + params.buffer_bps / 10_000)
    lower = range_lo * (1 - params.buffer_bps / 10_000)
    scan = continuous.iloc[params.opening_minutes:].reset_index(drop=True)
    pending: Optional[str] = None
    confirmations = 0
    signal_i: Optional[int] = None
    last_entry = _time(params.last_entry_time)
    for i, bar in scan.iterrows():
        if bar.time_key.time() > last_entry:
            break
        direction = "long" if bar.close > upper else "short" if bar.close < lower else None
        if direction == "long" and params.require_above_vwap:
            upto = continuous[continuous.time_key <= bar.time_key]
            volume = upto["volume"] if "volume" in upto else pd.Series(1.0, index=upto.index)
            typical = (upto.high + upto.low + upto.close) / 3
            vwap = float((typical * volume).sum() / volume.sum()) if volume.sum() else float(bar.close)
            if float(bar.close) <= vwap:
                direction = None
        if direction is None or (direction == "long" and not params.allow_long) or (
                direction == "short" and (not params.allow_short or not shortable)):
            pending, confirmations = None, 0
            continue
        if direction == pending:
            confirmations += 1
        else:
            pending, confirmations = direction, 1
        if confirmations >= params.confirm_bars:
            signal_i = i
            break
    if signal_i is None:
        return _empty("no_confirmed_breakout", trade_date)
    if signal_i + 1 >= len(scan):
        return _empty("no_next_bar_for_entry", trade_date)

    entry_bar = scan.iloc[signal_i + 1]
    raw_entry = float(entry_bar.open)
    adverse = params.slippage_bps_per_side / 10_000
    entry = raw_entry * (1 + adverse if pending == "long" else 1 - adverse)
    # Confirmation bars are part of the strategy, not slippage.  Only reject
    # an adverse gap from the confirming close to the next-bar fill.
    confirm_close = float(scan.iloc[signal_i].close)
    adverse_gap = max(0.0, entry - confirm_close) if pending == "long" else max(0.0, confirm_close - entry)
    chase_bps = adverse_gap / confirm_close * 10_000
    if (chase_bps > params.max_entry_slippage_bps or
            adverse_gap > band * params.max_chase_band_ratio):
        return _empty("entry_chase_too_large", trade_date)

    fee_per_share = entry * params.fee_bps_per_side * 2 / 10_000
    net_risk_per_share = band + fee_per_share + entry * params.slippage_bps_per_side * 2 / 10_000
    net_reward = band * params.reward_risk - fee_per_share - entry * params.slippage_bps_per_side * 2 / 10_000
    net_rr = net_reward / net_risk_per_share if net_risk_per_share else 0
    if net_rr < params.min_net_reward_risk:
        return _empty("net_reward_risk_too_low", trade_date)
    risk_qty = floor((equity * params.risk_per_trade) / net_risk_per_share)
    cash_qty = floor((equity * params.max_position_pct) / entry)
    qty = floor(min(risk_qty, cash_qty) / lot_size) * lot_size
    if qty < lot_size:
        return _empty("risk_budget_below_one_lot", trade_date)

    stop = entry - band if pending == "long" else entry + band
    target = entry + band * params.reward_risk if pending == "long" else entry - band * params.reward_risk
    exit_px, exit_reason, held = None, None, 0
    future = scan.iloc[signal_i + 1:]
    failed_count = 0
    for held, (_, bar) in enumerate(future.iterrows(), start=1):
        stop_hit = bar.low <= stop if pending == "long" else bar.high >= stop
        target_hit = bar.high >= target if pending == "long" else bar.low <= target
        if stop_hit:  # conservative handling of same-bar ambiguity
            exit_px, exit_reason = stop, "stop"
            break
        if target_hit:
            exit_px, exit_reason = target, "target"
            break
        if params.failed_breakout_exit:
            upto = continuous[continuous.time_key <= bar.time_key]
            volume = upto["volume"] if "volume" in upto else pd.Series(1.0, index=upto.index)
            typical = (upto.high + upto.low + upto.close) / 3
            vwap = float((typical * volume).sum() / volume.sum()) if volume.sum() else float(bar.close)
            failed = (bar.close < max(range_hi, vwap) if pending == "long"
                      else bar.close > min(range_lo, vwap))
            failed_count = failed_count + 1 if failed else 0
            if failed_count >= params.failed_breakout_bars:
                exit_px, exit_reason = float(bar.close), "failed_breakout"
                break
        if held >= params.max_hold_bars:
            exit_px, exit_reason = float(bar.close), "max_hold"
            break
        if bar.time_key.time() >= _time(params.force_exit_time):
            exit_px, exit_reason = float(bar.close), "session_close"
            break
    if exit_px is None:
        exit_px, exit_reason = float(future.iloc[-1].close), "last_bar"
    exit_px *= 1 - adverse if pending == "long" else 1 + adverse
    gross = (exit_px - entry) * qty * (1 if pending == "long" else -1)
    fees = (entry + exit_px) * qty * params.fee_bps_per_side / 10_000
    pnl = gross - fees
    risk_budget = net_risk_per_share * qty
    return {
        "traded": True, "trade_date": trade_date, "direction": pending,
        "signal_time": str(scan.iloc[signal_i].time_key),
        "entry_time": str(entry_bar.time_key), "entry": entry,
        "stop": stop, "target": target, "exit": exit_px,
        "exit_reason": exit_reason, "exit_bars": held, "qty": qty,
        "lot_size": lot_size, "range_hi": range_hi, "range_lo": range_lo,
        "range_bps": range_bps, "atr": atr, "band": band,
        "net_reward_risk": net_rr, "fees": fees, "pnl": pnl,
        "r_multiple": pnl / risk_budget if risk_budget else 0,
        "params": asdict(params),
    }


def backtest(frame: pd.DataFrame, params: OrbParams = OrbParams(), **kwargs) -> dict:
    """Backtest multiple dates and return portfolio metrics from daily equity."""
    df = _prepare(frame)
    trades = []
    equity = float(kwargs.pop("equity", 1_000_000.0))
    initial = equity
    daily = []
    for _, day in df.groupby(df.time_key.dt.date, sort=True):
        result = run_day(day, params, equity=equity, prepared=True, **kwargs)
        if result.get("traded"):
            equity += result["pnl"]
            trades.append(result)
        daily.append(equity)
    pnls = np.array([t["pnl"] for t in trades], dtype=float)
    rs = np.array([t["r_multiple"] for t in trades], dtype=float)
    curve = np.array([initial] + daily, dtype=float)
    peak = np.maximum.accumulate(curve)
    drawdown = curve / peak - 1
    wins = pnls[pnls > 0].sum() if len(pnls) else 0.0
    losses = -pnls[pnls < 0].sum() if len(pnls) else 0.0
    return {
        "params": asdict(params), "trades": trades, "trade_count": len(trades),
        "win_rate": float((pnls > 0).mean()) if len(pnls) else 0.0,
        "net_pnl": float(pnls.sum()), "return_pct": (equity / initial - 1) * 100,
        "profit_factor": float(wins / losses) if losses else (float("inf") if wins else 0.0),
        "expectancy_r": float(rs.mean()) if len(rs) else 0.0,
        "max_drawdown_pct": float(drawdown.min() * 100),
    }


def walk_forward(frame: pd.DataFrame, candidates: Iterable[OrbParams], *,
                 train_days: int = 60, test_days: int = 20, **kwargs) -> dict:
    """Rolling train/test selection. Score rejects tiny or fragile samples."""
    df = _prepare(frame)
    dates = list(df.time_key.dt.date.unique())
    folds, all_test_trades = [], []
    step = max(1, test_days)
    for start in range(0, len(dates) - train_days, step):
        train_set = set(dates[start:start + train_days])
        test_set = set(dates[start + train_days:start + train_days + test_days])
        if not test_set:
            break
        train = df[df.time_key.dt.date.isin(train_set)]
        ranked = []
        for p in candidates:
            report = backtest(train, p, **kwargs)
            n = report["trade_count"]
            score = report["expectancy_r"] * min(1.0, n / 20) + report["max_drawdown_pct"] / 100
            ranked.append((score, p, report))
        _, best, train_report = max(ranked, key=lambda x: x[0])
        test = df[df.time_key.dt.date.isin(test_set)]
        test_report = backtest(test, best, **kwargs)
        all_test_trades.extend(test_report["trades"])
        folds.append({"train_start": str(min(train_set)), "test_start": str(min(test_set)),
                      "params": asdict(best), "train": train_report, "test": test_report})
    pnls = [t["pnl"] for t in all_test_trades]
    rs = [t["r_multiple"] for t in all_test_trades]
    wins = sum(x for x in pnls if x > 0)
    losses = -sum(x for x in pnls if x < 0)
    return {"folds": folds, "test_trade_count": len(pnls),
            "test_net_pnl": sum(pnls),
            "test_win_rate": sum(x > 0 for x in pnls) / len(pnls) if pnls else 0,
            "test_expectancy_r": float(np.mean(rs)) if rs else 0,
            "test_profit_factor": wins / losses if losses else (float("inf") if wins else 0)}

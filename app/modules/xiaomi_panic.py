"""Five-minute panic-reversal strategy for Xiaomi (HK.01810).

Signals use completed five-minute bars. Entries occur at the next one-minute
open, so the implementation does not trade on information unavailable at the
fill time. The strategy is long-only and always flat before the HK close.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from math import floor

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class PanicParams:
    opening_minutes: int = 30
    stock_drop_pct: float = -1.2
    relative_weakness_pct: float = 0.8
    opening_volume_ratio: float = 1.5
    min_gap_pct: float = -4.0
    max_gap_pct: float = 1.0
    no_new_low_bars: int = 2
    confirmation_score: int = 4
    max_entry_time: str = "14:30"
    max_hold_minutes: int = 90
    reward_risk: float = 2.0
    risk_per_trade: float = 0.01
    max_investment_pct: float = 0.90
    fee_bps_per_side: float = 12.0
    slippage_bps_per_side: float = 5.0


def _five_minute(frame: pd.DataFrame) -> pd.DataFrame:
    x = frame.copy()
    x["time_key"] = pd.to_datetime(x.time_key)
    x = x.set_index("time_key").sort_index()
    out = x.resample("5min", origin="start_day", offset="0min").agg(
        open=("open", "first"), high=("high", "max"), low=("low", "min"),
        close=("close", "last"), volume=("volume", "sum"))
    return out.dropna().reset_index()


def _day_features(frame: pd.DataFrame) -> pd.DataFrame:
    x = _five_minute(frame)
    typical = (x.high + x.low + x.close) / 3
    x["vwap"] = (typical * x.volume).cumsum() / x.volume.cumsum()
    fast = x.close.ewm(span=12, adjust=False).mean()
    slow = x.close.ewm(span=26, adjust=False).mean()
    x["macd_hist"] = (fast - slow) - (fast - slow).ewm(span=9, adjust=False).mean()
    return x


def _metrics(trades: list, initial: float) -> dict:
    pnls = np.array([t["pnl"] for t in trades], dtype=float)
    rs = np.array([t["r_multiple"] for t in trades], dtype=float)
    wins = pnls[pnls > 0].sum() if len(pnls) else 0.0
    losses = -pnls[pnls < 0].sum() if len(pnls) else 0.0
    curve = initial + np.concatenate(([0.0], np.cumsum(pnls)))
    dd = curve / np.maximum.accumulate(curve) - 1
    return {"trade_count": len(trades), "net_pnl": float(pnls.sum()),
            "return_pct": float(pnls.sum() / initial * 100),
            "win_rate": float((pnls > 0).mean()) if len(pnls) else 0.0,
            "expectancy_r": float(rs.mean()) if len(rs) else 0.0,
            "profit_factor": float(wins / losses) if losses else (999.0 if wins else 0.0),
            "max_drawdown_pct": float(dd.min() * 100)}


def backtest(stock: pd.DataFrame, index: pd.DataFrame,
             params: PanicParams = PanicParams(), *, equity: float = 20_000,
             lot_size: int = 200) -> dict:
    s, ix = stock.copy(), index.copy()
    s["time_key"], ix["time_key"] = pd.to_datetime(s.time_key), pd.to_datetime(ix.time_key)
    stock_days = {d: g for d, g in s.groupby(s.time_key.dt.date, sort=True)}
    index_days = {d: g for d, g in ix.groupby(ix.time_key.dt.date, sort=True)}
    opening_volumes, previous_close, trades, rejected = [], None, [], {}
    account = float(equity)
    for date, minute in stock_days.items():
        idx_minute = index_days.get(date)
        if idx_minute is None:
            continue
        bars, idx = _day_features(minute), _day_features(idx_minute)
        open_count = params.opening_minutes // 5
        if len(bars) < open_count + 4 or len(idx) < open_count:
            continue
        opening, idx_opening = bars.iloc[:open_count], idx.iloc[:open_count]
        opening_volume = float(opening.volume.sum())
        prior_median = float(np.median(opening_volumes[-20:])) if len(opening_volumes) >= 10 else None
        opening_volumes.append(opening_volume)
        open_px = float(opening.iloc[0].open)
        gap_pct = (open_px / previous_close - 1) * 100 if previous_close else 0.0
        previous_close = float(bars.iloc[-1].close)
        stock_ret = (float(opening.iloc[-1].close) / open_px - 1) * 100
        index_ret = (float(idx_opening.iloc[-1].close) / float(idx_opening.iloc[0].open) - 1) * 100
        filters = {
            "stock_not_panicked": stock_ret > params.stock_drop_pct,
            "relative_weakness_small": index_ret - stock_ret < params.relative_weakness_pct,
            "opening_volume_low": prior_median is None or opening_volume < prior_median * params.opening_volume_ratio,
            "gap_outside": not (params.min_gap_pct <= gap_pct <= params.max_gap_pct),
        }
        failed = next((k for k, v in filters.items() if v), None)
        if failed:
            rejected[failed] = rejected.get(failed, 0) + 1
            continue
        opening_low = float(opening.low.min())
        signal_i = None
        for i in range(open_count + params.no_new_low_bars, len(bars) - 1):
            if bars.iloc[i].time_key.time() > pd.Timestamp(params.max_entry_time).time():
                break
            recent = bars.iloc[i - params.no_new_low_bars:i + 1]
            local_low = float(bars.iloc[open_count:i + 1].low.min())
            no_new_low = float(recent.iloc[-1].low) >= float(recent.iloc[:-1].low.min())
            hist_narrowing = (bars.iloc[i].macd_hist > bars.iloc[i - 1].macd_hist and
                              bars.iloc[i - 1].macd_hist < 0)
            reclaim = bars.iloc[i].close > bars.iloc[i].vwap
            break_prev = bars.iloc[i].close > bars.iloc[i - 1].high
            idx_so_far = idx[idx.time_key <= bars.iloc[i].time_key]
            index_holds = len(idx_so_far) and idx_so_far.iloc[-1].low > idx_so_far.low.min()
            confirmations = sum((no_new_low, hist_narrowing, reclaim,
                                 break_prev, index_holds))
            if confirmations >= params.confirmation_score and no_new_low:
                signal_i = i
                break
        if signal_i is None:
            rejected["no_reversal_confirmation"] = rejected.get("no_reversal_confirmation", 0) + 1
            continue
        signal_time = bars.iloc[signal_i].time_key
        future_minute = minute[minute.time_key > signal_time]
        if future_minute.empty:
            continue
        raw_entry = float(future_minute.iloc[0].open)
        entry = raw_entry * (1 + params.slippage_bps_per_side / 10_000)
        local_low = float(bars.iloc[open_count:signal_i + 1].low.min())
        tick = .02 if entry >= 10 else .01
        stop = local_low - 2 * tick
        price_risk = entry - stop
        cost_risk = entry * (2 * params.fee_bps_per_side + 2 * params.slippage_bps_per_side) / 10_000
        per_share_risk = price_risk + cost_risk
        qty_risk = floor(account * params.risk_per_trade / per_share_risk / lot_size) * lot_size
        qty_cash = floor(account * params.max_investment_pct / entry / lot_size) * lot_size
        qty = min(qty_risk, qty_cash)
        if qty < lot_size or stop >= entry:
            rejected["one_lot_risk_too_large"] = rejected.get("one_lot_risk_too_large", 0) + 1
            continue
        target = entry + price_risk * params.reward_risk
        exit_px, reason, held = float(future_minute.iloc[-1].close), "session_close", 0
        for _, bar in future_minute.iterrows():
            held += 1
            if bar.low <= stop:
                exit_px, reason = stop, "stop"; break
            if bar.high >= target:
                exit_px, reason = target, "target"; break
            if held >= params.max_hold_minutes:
                exit_px, reason = float(bar.close), "max_hold"; break
            if bar.time_key.time() >= pd.Timestamp("15:50").time():
                exit_px, reason = float(bar.close), "session_close"; break
        exit_px *= 1 - params.slippage_bps_per_side / 10_000
        pnl = ((exit_px - entry) * qty -
               (entry + exit_px) * qty * params.fee_bps_per_side / 10_000)
        risk_cash = per_share_risk * qty
        trade = {"date": str(date), "entry": entry, "stop": stop,
                 "target": target, "exit": exit_px, "qty": qty,
                 "pnl": pnl, "r_multiple": pnl / risk_cash,
                 "exit_reason": reason, "signal_time": str(signal_time)}
        trades.append(trade); account += pnl
    return {"params": asdict(params), "trades": trades,
            "rejected": rejected, **_metrics(trades, equity)}

"""Point-in-time daily mean-reversion research for Xiaomi (HK.01810).

Signals are formed from a completed daily bar and filled at the next open.
The module is research/read-only code and contains no order placement path.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from math import floor

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class MeanReversionParams:
    direction: str
    rsi2: float
    z20: float
    hold_days: int
    stop_pct: float
    regime: str = "any"


def prepare(stock: pd.DataFrame, index: pd.DataFrame) -> pd.DataFrame:
    s = stock.copy()
    i = index.copy()
    s["time_key"] = pd.to_datetime(s["time_key"])
    i["time_key"] = pd.to_datetime(i["time_key"])
    x = s.merge(i[["time_key", "close"]].rename(columns={"close": "idx_close"}),
                on="time_key", how="left").sort_values("time_key").reset_index(drop=True)
    close = x["close"].astype(float)
    delta = close.diff()
    up = delta.clip(lower=0).ewm(alpha=1 / 2, adjust=False).mean()
    down = (-delta.clip(upper=0)).ewm(alpha=1 / 2, adjust=False).mean()
    x["rsi2"] = 100 - 100 / (1 + up / down.replace(0, np.nan))
    x["ma5"] = close.rolling(5).mean()
    x["ma20"] = close.rolling(20).mean()
    x["ma50"] = close.rolling(50).mean()
    x["ma200"] = close.rolling(200).mean()
    x["z20"] = (close - x["ma20"]) / close.rolling(20).std(ddof=0)
    x["idx_ma60"] = x["idx_close"].rolling(60).mean()
    required = ["open", "high", "low", "close", "rsi2", "z20", "ma5", "ma50", "ma200"]
    return x.dropna(subset=required).reset_index(drop=True)


def signal(row: pd.Series, params: MeanReversionParams) -> bool:
    if params.regime == "up" and not row.close > row.ma200:
        return False
    if params.regime == "down" and not row.close < row.ma200:
        return False
    if params.regime == "not_strong_up" and not row.close < row.ma50:
        return False
    if params.direction == "long":
        return bool(row.rsi2 <= params.rsi2 and row.z20 <= -params.z20)
    if params.direction == "short":
        return bool(row.rsi2 >= 100 - params.rsi2 and row.z20 >= params.z20)
    raise ValueError(f"Unknown direction: {params.direction}")


def evaluate(x: pd.DataFrame, params: MeanReversionParams, *, initial_equity: float = 100_000,
             allocation_pct: float = 30, fee_bps: float = 12, slippage_bps: float = 8,
             annual_borrow_pct: float = 8) -> dict:
    """Single-position simulation with next-open entries/exits and adverse gap stops."""
    equity = float(initial_equity)
    peak = equity
    max_dd = 0.0
    trades: list[dict] = []
    i = 0
    side = 1 if params.direction == "long" else -1
    while i < len(x) - 2:
        if not signal(x.iloc[i], params):
            i += 1
            continue
        entry_i = i + 1
        raw_entry = float(x.iloc[entry_i].open)
        entry = raw_entry * (1 + side * slippage_bps / 10_000)
        qty = floor((equity * allocation_pct / 100) / entry / 200) * 200
        if qty < 200:
            i += 1
            continue
        stop = entry * (1 - side * params.stop_pct / 100)
        exit_i = min(entry_i + params.hold_days, len(x) - 1)
        reason = "max_hold"
        exit_price = None
        for j in range(entry_i, exit_i + 1):
            row = x.iloc[j]
            stop_hit = row.low <= stop if side == 1 else row.high >= stop
            if stop_hit:
                raw_exit = min(float(row.open), stop) if side == 1 else max(float(row.open), stop)
                exit_price = raw_exit * (1 - side * slippage_bps / 10_000)
                exit_i, reason = j, "stop"
                break
            mean_exit = row.close >= row.ma5 if side == 1 else row.close <= row.ma5
            if mean_exit and j < len(x) - 1:
                exit_i = j + 1
                exit_price = float(x.iloc[exit_i].open) * (1 - side * slippage_bps / 10_000)
                reason = "mean_reversion"
                break
        if exit_price is None:
            exit_price = float(x.iloc[exit_i].close) * (1 - side * slippage_bps / 10_000)
        gross = side * (exit_price - entry) * qty
        fees = (entry + exit_price) * qty * fee_bps / 10_000
        days = max(1, (x.iloc[exit_i].time_key - x.iloc[entry_i].time_key).days)
        borrow = entry * qty * annual_borrow_pct / 100 * days / 365 if side == -1 else 0.0
        pnl = gross - fees - borrow
        equity += pnl
        peak = max(peak, equity)
        max_dd = min(max_dd, equity / peak - 1)
        trades.append({"entry_date": str(x.iloc[entry_i].time_key.date()),
                       "exit_date": str(x.iloc[exit_i].time_key.date()),
                       "entry": entry, "exit": exit_price, "qty": qty,
                       "pnl": pnl, "return_on_equity_pct": pnl / initial_equity * 100,
                       "reason": reason})
        i = exit_i + 1
    wins = sum(t["pnl"] for t in trades if t["pnl"] > 0)
    losses = -sum(t["pnl"] for t in trades if t["pnl"] < 0)
    returns = np.array([t["return_on_equity_pct"] / 100 for t in trades])
    return {"params": asdict(params), "count": len(trades), "trades": trades,
            "return_pct": (equity / initial_equity - 1) * 100,
            "profit_factor": wins / losses if losses else (999.0 if wins else 0.0),
            "win_rate": float(np.mean(returns > 0)) if len(returns) else 0.0,
            "expectancy_pct": float(np.mean(returns) * 100) if len(returns) else 0.0,
            "max_drawdown_pct": max_dd * 100}


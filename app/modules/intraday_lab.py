"""Small, conservative intraday strategy lab for one-minute HK bars."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from math import floor

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class LabParams:
    family: str
    a: float
    b: float
    stop_bps: float
    reward_risk: float
    max_hold: int
    start: str = "09:50"
    last_entry: str = "14:30"


def features(frame: pd.DataFrame) -> pd.DataFrame:
    x = frame.copy()
    x["time_key"] = pd.to_datetime(x.time_key)
    x = x.sort_values("time_key").drop_duplicates("time_key")
    typical = (x.high + x.low + x.close) / 3
    day = x.time_key.dt.date
    x["vwap"] = (typical * x.volume).groupby(day).cumsum() / x.volume.groupby(day).cumsum()
    x["ema9"] = x.close.groupby(day).transform(lambda s: s.ewm(span=9, adjust=False).mean())
    x["ema21"] = x.close.groupby(day).transform(lambda s: s.ewm(span=21, adjust=False).mean())
    fast = x.close.groupby(day).transform(lambda s: s.ewm(span=12, adjust=False).mean())
    slow = x.close.groupby(day).transform(lambda s: s.ewm(span=26, adjust=False).mean())
    x["macd"] = fast - slow
    x["macd_signal"] = x.macd.groupby(day).transform(lambda s: s.ewm(span=9, adjust=False).mean())
    ret = x.close.groupby(day).pct_change()
    x["vol20"] = ret.groupby(day).transform(lambda s: s.rolling(20, min_periods=10).std())
    x["dev_vwap_bps"] = (x.close / x.vwap - 1) * 10000
    x["vol_med20"] = x.volume.groupby(day).transform(lambda s: s.rolling(20, min_periods=10).median())
    return x


def _signal(day: pd.DataFrame, i: int, p: LabParams, opening_hi: float, opening_lo: float):
    r, prev = day.iloc[i], day.iloc[i - 1]
    if p.family == "vwap_reversion":
        # Recovering from an extreme below VWAP; b is minimum volume ratio.
        return (prev.dev_vwap_bps <= -p.a and r.close > prev.close and
                r.volume >= r.vol_med20 * p.b)
    if p.family == "macd_trend":
        # Trend-aligned bullish MACD cross; a is minimum EMA spread in bps.
        spread = (r.ema9 / r.ema21 - 1) * 10000
        return (spread >= p.a and prev.macd <= prev.macd_signal and
                r.macd > r.macd_signal and r.close > r.vwap)
    if p.family == "failed_breakdown":
        # Previous bar broke opening low, current closes back inside; a=buffer.
        level = opening_lo * (1 - p.a / 10000)
        return prev.low < level and r.close > opening_lo and r.close > r.vwap
    raise ValueError(p.family)


def run_day(day: pd.DataFrame, p: LabParams, *, equity=20000.0, lot_size=200,
            fee_bps=12.0, slippage_bps=5.0):
    d = day.copy().reset_index(drop=True)
    continuous = d[((d.time_key.dt.time >= pd.Timestamp("09:30").time()) &
                    (d.time_key.dt.time < pd.Timestamp("12:00").time())) |
                   ((d.time_key.dt.time >= pd.Timestamp("13:00").time()) &
                    (d.time_key.dt.time <= pd.Timestamp("15:55").time()))].reset_index(drop=True)
    if len(continuous) < 30: return None
    opening = continuous.iloc[:15]
    hi, lo = float(opening.high.max()), float(opening.low.min())
    start, last = pd.Timestamp(p.start).time(), pd.Timestamp(p.last_entry).time()
    signal_i = None
    for i in range(20, len(continuous) - 1):
        t = continuous.iloc[i].time_key.time()
        if t < start: continue
        if t > last: break
        if _signal(continuous, i, p, hi, lo): signal_i = i; break
    if signal_i is None: return None
    entry_bar = continuous.iloc[signal_i + 1]
    entry = float(entry_bar.open) * (1 + slippage_bps / 10000)
    stop_dist = entry * p.stop_bps / 10000
    risk_share = stop_dist + entry * (fee_bps * 2 + slippage_bps * 2) / 10000
    qty = floor((equity * .01) / risk_share / lot_size) * lot_size
    cash_qty = floor(equity / entry / lot_size) * lot_size
    qty = min(qty, cash_qty)
    if qty < lot_size: return None
    stop, target = entry - stop_dist, entry + stop_dist * p.reward_risk
    exit_px = float(continuous.iloc[-1].close); reason = "session_close"
    held = 0
    for held, (_, bar) in enumerate(continuous.iloc[signal_i + 1:].iterrows(), 1):
        if bar.low <= stop: exit_px, reason = stop, "stop"; break
        if bar.high >= target: exit_px, reason = target, "target"; break
        if p.family == "vwap_reversion" and bar.close >= bar.vwap:
            exit_px, reason = float(bar.close), "vwap"; break
        if held >= p.max_hold: exit_px, reason = float(bar.close), "max_hold"; break
    exit_px *= 1 - slippage_bps / 10000
    gross = (exit_px - entry) * qty
    fees = (entry + exit_px) * qty * fee_bps / 10000
    pnl = gross - fees
    return {"date": str(continuous.iloc[0].time_key.date()), "pnl": pnl,
            "r": pnl / (risk_share * qty), "qty": qty, "reason": reason}


def evaluate(frame: pd.DataFrame, p: LabParams, **kwargs):
    trades=[]; equity=float(kwargs.pop("equity",20000))
    for _,day in frame.groupby(frame.time_key.dt.date,sort=True):
        t=run_day(day,p,equity=equity,**kwargs)
        if t: trades.append(t); equity+=t["pnl"]
    wins=sum(t["pnl"] for t in trades if t["pnl"]>0)
    losses=-sum(t["pnl"] for t in trades if t["pnl"]<0)
    rs=[t["r"] for t in trades]
    return {"params":asdict(p),"trades":trades,"trade_count":len(trades),
            "net_pnl":sum(t["pnl"] for t in trades),
            "expectancy_r":float(np.mean(rs)) if rs else 0,
            "profit_factor":wins/losses if losses else (999 if wins else 0),
            "win_rate":sum(t["pnl"]>0 for t in trades)/len(trades) if trades else 0}

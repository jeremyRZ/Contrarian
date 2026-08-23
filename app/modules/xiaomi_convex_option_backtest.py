"""Point-in-time backtest for convex Xiaomi call/put entries.

The signal is formed from the underlying close.  Option entry therefore uses
the next trading day's HKEX settlement, never the signal day's settlement.
"""
from __future__ import annotations

from dataclasses import dataclass
from itertools import product

import numpy as np
import pandas as pd

from .supertrend_research import SuperTrendParams, supertrend


@dataclass(frozen=True)
class ConvexSignal:
    signal_date: str
    entry_date: str
    entry_index: int
    direction: int
    kind: str
    spot: float


def build_convex_signals(stock: pd.DataFrame) -> list[ConvexSignal]:
    x = stock.copy()
    x["time_key"] = pd.to_datetime(x.time_key)
    x = x[x.time_key >= "2021-01-01"].sort_values("time_key").reset_index(drop=True)
    previous_close = x.close.shift(1)
    true_range = pd.concat([
        x.high - x.low,
        (x.high - previous_close).abs(),
        (x.low - previous_close).abs(),
    ], axis=1).max(axis=1)
    atr14 = true_range.rolling(14).mean()
    ret1 = x.close.pct_change()
    high55 = x.high.shift(1).rolling(55).max()
    low55 = x.low.shift(1).rolling(55).min()
    signals: list[ConvexSignal] = []
    for i in range(55, len(x) - 1):
        direction = 0
        kind = ""
        if x.loc[i, "close"] > high55.iloc[i]:
            direction, kind = 1, "breakout55"
        elif x.loc[i, "close"] < low55.iloc[i]:
            direction, kind = -1, "breakout55"
        elif abs(ret1.iloc[i]) > .045 and true_range.iloc[i] / atr14.iloc[i] > 1.5:
            direction, kind = (1 if ret1.iloc[i] > 0 else -1), "large_move"
        if direction:
            entry_i = i + 1
            signals.append(ConvexSignal(
                str(x.loc[i, "time_key"].date()), str(x.loc[entry_i, "time_key"].date()),
                entry_i, direction, kind, float(x.loc[i, "close"]),
            ))
    return signals


def build_supertrend_signals(stock: pd.DataFrame, *, mode: str,
                             atr_period: int, multiplier: float) -> list[ConvexSignal]:
    """Build close-known directional events for option research."""
    if mode not in {"breakout_confirm", "breakout_recent_flip",
                    "momentum_confirm", "flip_with_momentum"}:
        raise ValueError(mode)
    x = stock.copy()
    x["time_key"] = pd.to_datetime(x.time_key)
    x = x[x.time_key >= "2021-01-01"].sort_values("time_key").reset_index(drop=True)
    direction = supertrend(x, SuperTrendParams(atr_period, multiplier))["st_direction"]
    flip = direction.ne(direction.shift(1)) & direction.ne(0)
    last_flip = pd.Series(np.nan, index=x.index)
    last = None
    for i, value in enumerate(flip):
        if value:
            last = i
        last_flip.iloc[i] = last
    momentum = x.close.pct_change(20)
    prior_high = x.high.shift(1).rolling(55).max()
    prior_low = x.low.shift(1).rolling(55).min()
    signals = []
    for i in range(55, len(x) - 1):
        trend = int(direction.iloc[i])
        breakout = 1 if x.close.iloc[i] > prior_high.iloc[i] else (
            -1 if x.close.iloc[i] < prior_low.iloc[i] else 0)
        mom = 1 if momentum.iloc[i] > .05 else (-1 if momentum.iloc[i] < -.05 else 0)
        signal_direction = 0
        if mode == "breakout_confirm" and breakout == trend:
            signal_direction = breakout
        elif (mode == "breakout_recent_flip" and breakout == trend
              and pd.notna(last_flip.iloc[i]) and i - int(last_flip.iloc[i]) <= 10):
            signal_direction = breakout
        elif mode == "momentum_confirm" and mom == trend and mom != 0:
            previous_mom = 1 if momentum.iloc[i - 1] > .05 else (
                -1 if momentum.iloc[i - 1] < -.05 else 0)
            if previous_mom != mom:
                signal_direction = mom
        elif mode == "flip_with_momentum" and bool(flip.iloc[i]) and mom == trend:
            signal_direction = trend
        if signal_direction:
            signals.append(ConvexSignal(
                str(x.time_key.iloc[i].date()), str(x.time_key.iloc[i + 1].date()),
                i + 1, signal_direction, mode, float(x.close.iloc[i])))
    return signals


def parameter_grid() -> list[dict]:
    return [
        {"kind": kind, "dte": dte, "otm_pct": otm, "hold": hold}
        for kind, dte, otm, hold in product(
            ("breakout55", "large_move", "all"),
            ((7, 30), (15, 45), (30, 60)),
            (0, 5, 10, 15),
            (1, 2, 3, 5, 10),
        )
    ]


def evaluate(signal: ConvexSignal, stock: pd.DataFrame, frames: dict[str, pd.DataFrame],
             *, dte: tuple[int, int], otm_pct: int, hold: int,
             slippage_pct: float = 5.0, min_turnover: float = 1,
             min_oi: float = 10) -> dict | None:
    entry_key = signal.entry_date.replace("-", "")
    exit_i = signal.entry_index + hold
    if exit_i >= len(stock):
        return None
    exit_date = str(pd.Timestamp(stock.loc[exit_i, "time_key"]).date())
    exit_key = exit_date.replace("-", "")
    entry, exit_ = frames.get(entry_key), frames.get(exit_key)
    if entry is None or exit_ is None:
        return None
    entry_date = pd.Timestamp(signal.entry_date)
    possible = entry[(entry.expiry - entry_date).dt.days.between(*dte)]
    expiries = possible.expiry.unique()
    if not len(expiries):
        return None
    expiry = min(expiries)
    side = "call" if signal.direction > 0 else "put"
    target = signal.spot * (1 + signal.direction * otm_pct / 100)
    chain = possible[possible.expiry == expiry].copy()
    chain = chain[(chain[f"{side}_turnover"] >= min_turnover)
                  & (chain[f"{side}_oi"] >= min_oi)
                  & (chain[f"{side}_settle"] > 0)]
    if chain.empty:
        return None
    row = chain.loc[(chain.strike - target).abs().idxmin()]
    if abs(float(row.strike) / target - 1) > .05:
        return None
    matched = exit_[(exit_.expiry == expiry) & (exit_.strike == row.strike)]
    if matched.empty:
        return None
    entry_price = float(row[f"{side}_settle"])
    exit_price = float(matched.iloc[0][f"{side}_settle"])
    if entry_price <= 0 or exit_price < 0:
        return None
    paid = entry_price * (1 + slippage_pct / 100)
    received = exit_price * (1 - slippage_pct / 100)
    return {
        "signal_date": signal.signal_date, "entry_date": signal.entry_date,
        "exit_date": exit_date, "kind": signal.kind,
        "direction": signal.direction, "side": side, "expiry": str(expiry.date()),
        "strike": float(row.strike), "spot": signal.spot,
        "entry_settle": entry_price, "exit_settle": exit_price,
        "return_pct": (received / paid - 1) * 100,
    }


def metrics(rows: list[dict]) -> dict:
    if not rows:
        return {"count": 0}
    values = np.asarray([row["return_pct"] / 100 for row in rows], dtype=float)
    # One percent of capital premium at risk per trade; a long option cannot
    # lose more than the premium, and overlapping positions are ignored here.
    curve = np.cumprod(1 + np.maximum(values, -1) * .01)
    drawdown = curve / np.maximum.accumulate(curve) - 1
    losses = -values[values < 0].sum()
    return {
        "count": int(len(values)), "win_rate": float((values > 0).mean()),
        "mean_return_pct": float(values.mean() * 100),
        "median_return_pct": float(np.median(values) * 100),
        "max_trade_return_pct": float(values.max() * 100),
        "min_trade_return_pct": float(values.min() * 100),
        "profit_factor": float(values[values > 0].sum() / losses) if losses else 999.0,
        "portfolio_return_pct": float((curve[-1] - 1) * 100),
        "max_drawdown_pct": float(drawdown.min() * 100),
    }


def non_overlapping(rows: list[dict]) -> list[dict]:
    """Keep at most one option position open; discard clustered duplicate entries."""
    kept, last_exit = [], None
    for row in sorted(rows, key=lambda item: (item["entry_date"], item["exit_date"])):
        entry = pd.Timestamp(row["entry_date"])
        if last_exit is not None and entry <= last_exit:
            continue
        kept.append(row)
        last_exit = pd.Timestamp(row["exit_date"])
    return kept

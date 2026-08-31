"""No-lookahead SuperTrend overlays for directional-strategy research.

Signals are formed after a daily close and positions take effect at the next
open.  This module is research-only; it does not submit or recommend orders.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from app.hk_costs import effective_rate


@dataclass(frozen=True)
class SuperTrendParams:
    atr_period: int = 10
    multiplier: float = 3.0


def supertrend(bars: pd.DataFrame, params: SuperTrendParams) -> pd.DataFrame:
    """Return ATR, final bands and direction (1 bullish, -1 bearish).

    ATR uses Wilder's recursive moving average.  Every value at row ``t`` is
    derived exclusively from rows ``<= t``.
    """
    if params.atr_period < 2 or params.multiplier <= 0:
        raise ValueError("atr_period must be >= 2 and multiplier must be positive")
    required = {"high", "low", "close"}
    missing = required.difference(bars.columns)
    if missing:
        raise ValueError(f"missing columns: {sorted(missing)}")

    high = pd.to_numeric(bars["high"], errors="coerce")
    low = pd.to_numeric(bars["low"], errors="coerce")
    close = pd.to_numeric(bars["close"], errors="coerce")
    prev_close = close.shift(1)
    tr = pd.concat((high - low, (high - prev_close).abs(),
                    (low - prev_close).abs()), axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / params.atr_period, adjust=False,
                 min_periods=params.atr_period).mean()
    midpoint = (high + low) / 2
    basic_upper = midpoint + params.multiplier * atr
    basic_lower = midpoint - params.multiplier * atr

    upper = pd.Series(np.nan, index=bars.index, dtype=float)
    lower = pd.Series(np.nan, index=bars.index, dtype=float)
    direction = pd.Series(0, index=bars.index, dtype=int)
    valid = np.flatnonzero(atr.notna().to_numpy())
    if not len(valid):
        return pd.DataFrame({"atr": atr, "supertrend": np.nan,
                             "upper_band": upper, "lower_band": lower,
                             "st_direction": direction}, index=bars.index)

    first = int(valid[0])
    upper.iloc[first] = basic_upper.iloc[first]
    lower.iloc[first] = basic_lower.iloc[first]
    direction.iloc[first] = 1 if close.iloc[first] >= midpoint.iloc[first] else -1
    for i in range(first + 1, len(bars)):
        if pd.isna(atr.iloc[i]):
            continue
        upper.iloc[i] = (basic_upper.iloc[i]
                         if basic_upper.iloc[i] < upper.iloc[i - 1]
                         or close.iloc[i - 1] > upper.iloc[i - 1]
                         else upper.iloc[i - 1])
        lower.iloc[i] = (basic_lower.iloc[i]
                         if basic_lower.iloc[i] > lower.iloc[i - 1]
                         or close.iloc[i - 1] < lower.iloc[i - 1]
                         else lower.iloc[i - 1])
        prior = int(direction.iloc[i - 1])
        if close.iloc[i] > upper.iloc[i - 1]:
            direction.iloc[i] = 1
        elif close.iloc[i] < lower.iloc[i - 1]:
            direction.iloc[i] = -1
        else:
            direction.iloc[i] = prior
    line = lower.where(direction == 1, upper)
    return pd.DataFrame({"atr": atr, "supertrend": line,
                         "upper_band": upper, "lower_band": lower,
                         "st_direction": direction}, index=bars.index)


def combine_states(base: pd.Series, trend: pd.Series, mode: str) -> pd.Series:
    """Combine a base desired state with SuperTrend using explicit semantics."""
    if mode == "baseline":
        return base.astype(int)
    if mode == "standalone":
        return trend.astype(int)
    if mode not in {"entry_confirmation", "exit_overlay", "hybrid"}:
        raise ValueError(mode)

    states: list[int] = []
    held = 0
    lockout = 0
    for raw_base, raw_trend in zip(base.fillna(0), trend.fillna(0)):
        target, regime = int(raw_base), int(raw_trend)
        if lockout and target != lockout:
            lockout = 0
        agrees = target != 0 and target == regime

        if held == 0:
            if lockout == target:
                held = 0
            elif mode == "exit_overlay":
                held = target
            elif agrees:
                held = target
        elif target != held:
            held = target if (mode == "exit_overlay" or target == regime) else 0
        elif mode in {"exit_overlay", "hybrid"} and regime == -held:
            lockout, held = held, 0
        states.append(held)
    return pd.Series(states, index=base.index, dtype=int)


def evaluate_positions(bars: pd.DataFrame, desired: pd.Series, *,
                       allocation: float = 0.30, fee_bps: float | None = None,
                       slippage_bps: float = 8,
                       capital_hkd: float = 20_000,
                       annual_borrow_pct: float = 8) -> dict:
    """Evaluate close signals at the next open under the existing cost model."""
    position = desired.shift(1).fillna(0).astype(int)
    open_price = pd.to_numeric(bars["open"], errors="coerce")
    next_open_return = open_price.shift(-1) / open_price - 1
    turnover = position.diff().abs().fillna(position.abs())
    rate = (effective_rate(capital_hkd * allocation, slippage_bps)
            if fee_bps is None else (fee_bps + slippage_bps) / 10_000)
    costs = turnover * rate * allocation
    borrow = (position < 0).astype(float) * annual_borrow_pct / 100 / 252 * allocation
    returns = (position * next_open_return * allocation - costs - borrow).iloc[:-1]
    returns = returns.dropna()
    if returns.empty:
        raise ValueError("not enough valid bars to evaluate")
    equity = (1 + returns).cumprod()
    drawdown = equity / equity.cummax() - 1
    downside = returns[returns < 0]
    std, down_std = returns.std(ddof=1), downside.std(ddof=1)
    entries = ((position != position.shift(1)) & (position != 0)).iloc[:-1]
    gross_win, gross_loss = returns[returns > 0].sum(), -returns[returns < 0].sum()
    return {
        "count": int(entries.sum()),
        "return_pct": float((equity.iloc[-1] - 1) * 100),
        "annualized_pct": float((equity.iloc[-1] ** (252 / len(returns)) - 1) * 100),
        "sharpe": float(returns.mean() / std * np.sqrt(252)) if std else 0.0,
        "sortino": float(returns.mean() / down_std * np.sqrt(252)) if down_std else 0.0,
        "profit_factor": float(gross_win / gross_loss) if gross_loss else 999.0,
        "max_drawdown_pct": float(drawdown.min() * 100),
        "turnover_units": float(turnover.iloc[:-1].sum()),
        "long_days": int((position.iloc[:-1] > 0).sum()),
        "short_days": int((position.iloc[:-1] < 0).sum()),
        "flat_days": int((position.iloc[:-1] == 0).sum()),
    }

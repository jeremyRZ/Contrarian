"""Reproducible audit of conflicting Xiaomi daily signals.

This script does not place orders or change production parameters.  It compares
the frozen MA20/MA60 long-only state and the 20-day momentum state with the same
next-open timing, exposure, transaction costs, and short borrow assumption.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / ".universal_daily_60" / "HK_01810.csv"
OUTPUT = ROOT / "data" / "xiaomi_signal_conflict_audit.json"


def states(frame: pd.DataFrame) -> pd.DataFrame:
    x = frame.sort_values("time_key").drop_duplicates("time_key").copy()
    x["time_key"] = pd.to_datetime(x["time_key"])
    x["ma20"] = x.close.rolling(20).mean()
    x["ma60"] = x.close.rolling(60).mean()
    x["mom20"] = x.close.pct_change(20)

    trend, held, peak = [], False, np.nan
    for row in x.itertuples():
        if not held and row.close > row.ma60 and row.ma20 > row.ma60:
            held, peak = True, float(row.high)
        elif held:
            peak = max(float(peak), float(row.high))
            if row.close < row.ma20 or row.close < peak * 0.85:
                held, peak = False, np.nan
        trend.append(1 if held else 0)
    x["trend_state"] = trend
    x["momentum_state"] = np.select(
        [x.mom20 > 0.05, x.mom20 < -0.05], [1, -1], default=0
    ).astype(int)
    return x.dropna(subset=["ma60", "mom20"]).reset_index(drop=True)


def evaluate(x: pd.DataFrame, desired: pd.Series, start: str) -> dict:
    mask = x.time_key >= start
    y = x.loc[mask].copy()
    signal = desired.loc[y.index].astype(int)
    position = signal.shift(1).fillna(0).astype(int)
    next_open_return = y.open.shift(-1) / y.open - 1
    turnover = position.diff().abs().fillna(position.abs())
    allocation = 0.30
    returns = position * next_open_return * allocation
    returns -= turnover * 0.0020 * allocation
    returns -= (position < 0).astype(float) * 0.08 / 252 * allocation
    returns = returns.iloc[:-1]
    equity = (1 + returns).cumprod()
    drawdown = equity / equity.cummax() - 1
    std = returns.std(ddof=1)
    gross_win = returns[returns > 0].sum()
    gross_loss = -returns[returns < 0].sum()
    return {
        "return_pct": float((equity.iloc[-1] - 1) * 100),
        "annualized_pct": float((equity.iloc[-1] ** (252 / len(returns)) - 1) * 100),
        "sharpe": float(returns.mean() / std * np.sqrt(252)) if std else 0.0,
        "max_drawdown_pct": float(drawdown.min() * 100),
        "profit_factor": float(gross_win / gross_loss) if gross_loss else None,
        "turnovers": int((turnover.iloc[:-1] > 0).sum()),
    }


def conflict_forward_returns(x: pd.DataFrame, start: str) -> dict:
    conflict = (x.trend_state == 1) & (x.momentum_state == -1) & (x.time_key >= start)
    rows = x.loc[conflict]
    result = {"signal_days": int(conflict.sum()), "episodes": 0}
    result["episodes"] = int((conflict & ~conflict.shift(1, fill_value=False)).sum())
    for horizon in (1, 5, 10, 20):
        forward = x.open.shift(-(horizon + 1)) / x.open.shift(-1) - 1
        sample = forward.loc[rows.index].dropna()
        result[f"next_open_{horizon}d"] = {
            "observations": int(len(sample)),
            "mean_pct": float(sample.mean() * 100),
            "median_pct": float(sample.median() * 100),
            "positive_pct": float((sample > 0).mean() * 100),
        }
    return result


def main() -> None:
    x = states(pd.read_csv(DATA))
    trend = x.trend_state
    momentum = x.momentum_state
    variants = {
        "trend_long_only": trend,
        "momentum_long_flat_short": momentum,
        "trend_with_momentum_veto": ((trend == 1) & (momentum != -1)).astype(int),
        "strict_consensus_long_only": ((trend == 1) & (momentum == 1)).astype(int),
        "hierarchical_long_else_momentum_short": np.where(
            trend == 1, 1, np.where(momentum == -1, -1, 0)
        ),
    }
    result = {
        "data_range": [str(x.time_key.min().date()), str(x.time_key.max().date())],
        "method": {
            "execution": "close signal, next open position",
            "allocation_pct": 30,
            "fee_plus_slippage_bps_per_position_change": 20,
            "short_borrow_pct_annual": 8,
        },
        "periods": {},
        "conflict_condition": {},
        "latest": {
            "as_of": str(x.iloc[-1].time_key.date()),
            "close": float(x.iloc[-1].close),
            "ma20": float(x.iloc[-1].ma20),
            "ma60": float(x.iloc[-1].ma60),
            "momentum_20d_pct": float(x.iloc[-1].mom20 * 100),
            "trend_state": int(x.iloc[-1].trend_state),
            "momentum_state": int(x.iloc[-1].momentum_state),
        },
    }
    for start in ("2024-01-01", "2025-01-01"):
        result["periods"][start] = {
            name: evaluate(x, pd.Series(state, index=x.index), start)
            for name, state in variants.items()
        }
        result["conflict_condition"][start] = conflict_forward_returns(x, start)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

"""Risk-budget strategies without pretending sparse forward data are mature."""
from __future__ import annotations

import numpy as np
import pandas as pd


def risk_parity_weights(returns: pd.DataFrame) -> dict[str, float]:
    """Long-only equal-risk-contribution weights from aligned return series."""
    frame = returns.dropna()
    if frame.shape[0] < 5 or frame.shape[1] < 2:
        raise ValueError("至少需要2个策略、5个对齐收益样本")
    cov = frame.cov().to_numpy(dtype=float) + np.eye(frame.shape[1]) * 1e-10
    w = np.repeat(1 / len(cov), len(cov))
    for _ in range(1000):
        port_var = float(w @ cov @ w)
        rc = w * (cov @ w)
        target = port_var / len(w)
        updated = w * np.sqrt(target / np.maximum(rc, 1e-12))
        updated /= updated.sum()
        if np.max(np.abs(updated - w)) < 1e-9:
            w = updated
            break
        w = updated
    return {str(code): round(float(weight), 6) for code, weight in zip(frame.columns, w)}


def build_allocation(strategies: list[dict], evaluations: list[dict]) -> dict:
    """Use ERC when evidence is aligned; otherwise publish a labelled DD proxy."""
    records = []
    for item in evaluations:
        value = (item.get("returns") or {}).get("20")
        if value is not None:
            records.append({"date": item.get("signal_date"),
                            "strategy": item.get("strategy_id"), "return": float(value) / 100})
    if records:
        pivot = pd.DataFrame(records).groupby(["date", "strategy"])["return"].mean().unstack()
        try:
            weights = risk_parity_weights(pivot)
            return {"state": "ACTIVE", "method": "对齐20日收益等风险贡献",
                    "weights": weights, "aligned_samples": int(len(pivot)),
                    "limitation": "仅使用已发生的前向收益，不使用未来数据"}
        except ValueError:
            pass
    eligible = [s for s in strategies if s.get("action") != "UNAVAILABLE"]
    inv = {}
    for strategy in eligible:
        dd = abs(float((strategy.get("validation") or {}).get("max_drawdown_pct") or 0))
        if dd > 0:
            inv[strategy["id"]] = 1 / dd
    total = sum(inv.values())
    weights = {code: round(value / total, 6) for code, value in inv.items()} if total else {}
    return {"state": "COLLECTING", "method": "历史最大回撤倒数代理",
            "weights": weights, "aligned_samples": 0,
            "limitation": "前向样本不足，当前不是正式风险平价；达到至少5个对齐样本后自动切换"}

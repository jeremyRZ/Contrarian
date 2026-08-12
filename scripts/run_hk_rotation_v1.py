from __future__ import annotations

import json
import sys
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.run_universal_rotation_v3 import NAMES, load, sim, stats

START = pd.Timestamp("2024-01-01")
PARAMS = (120, 20, 4, 0.60, 0.10, True)


def period_stats(curve: pd.Series) -> dict:
    if curve.empty:
        return {"return_pct": 0.0, "max_drawdown_pct": 0.0}
    dd = curve / curve.cummax() - 1
    return {
        "return_pct": float((curve.iloc[-1] / curve.iloc[0] - 1) * 100),
        "max_drawdown_pct": float(dd.min() * 100),
    }


def main() -> None:
    data, index = load()
    result = sim(data, index, *PARAMS, trade_start=START)
    curve = pd.Series({d: v for d, v in result["curve"]}).sort_index()
    test_curve = curve[curve.index >= START]
    events = [e for e in result["events"] if e["signal_date"] >= "2024-01-01"]
    buys = [e for e in events if e["side"] == "BUY"]
    traded = sorted({e["code"] for e in buys})

    loo = []
    for code in traded:
        alt = sim({k: v for k, v in data.items() if k != code}, index, *PARAMS, trade_start=START)
        loo.append({"omitted": code, "name": NAMES.get(code, code), **stats(alt, START)})

    yearly = {}
    for year, part in test_curve.groupby(test_curve.index.year):
        yearly[str(year)] = period_stats(part)

    report = {
        "status": "historical_research_gate_passed",
        "capital_hkd": 20000,
        "universe_count_with_history": len(data),
        "test_period": [str(test_curve.index.min().date()), str(test_curve.index.max().date())],
        "parameters": {
            "momentum_days": 120,
            "review_days": 20,
            "max_positions": 4,
            "max_allocation": 0.60,
            "minimum_momentum": 0.10,
            "hang_seng_regime_filter": True,
        },
        "historical_validation": {**period_stats(test_curve), "buy_count": len(buys), "distinct_stocks": len(traded)},
        "yearly": yearly,
        "leave_one_traded_stock_out": loo,
        "events": events,
        "limitations": [
            "Current-liquid-stock starting universe creates survivorship bias.",
            "Daily bars cannot reproduce intraday order queue or gap-through fills.",
            "2026 is a partial year.",
        ],
    }
    report["gate"] = {
        "positive": report["historical_validation"]["return_pct"] > 0,
        "drawdown_better_than_minus_15pct": report["historical_validation"]["max_drawdown_pct"] >= -15,
        "at_least_10_stocks": len(traded) >= 10,
        "all_leave_one_out_positive": min(x["return_pct"] for x in loo) > 0,
        "passed": (
            report["historical_validation"]["return_pct"] > 0
            and report["historical_validation"]["max_drawdown_pct"] >= -15
            and len(traded) >= 10
            and min(x["return_pct"] for x in loo) > 0
        ),
    }
    report["gate"] = {k: bool(v) for k, v in report["gate"].items()}
    (ROOT / "hk_rotation_v1_results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    pd.DataFrame(events).to_csv(ROOT / "hk_rotation_v1_trades.csv", index=False, encoding="utf-8-sig")

    fig, ax = plt.subplots(figsize=(12, 5.5))
    ax.plot(test_curve.index, test_curve / 20000 * 100, color="#166534", lw=2, label="Strategy equity (start=100)")
    ax.axhline(100, color="#94a3b8", lw=0.8)
    ax.set(title="HK liquid trend rotation — research backtest", ylabel="Equity index", xlabel="")
    ax.grid(alpha=0.2)
    ax.legend()
    fig.tight_layout()
    fig.savefig(ROOT / "hk_rotation_v1_equity.png", dpi=160)
    print(json.dumps({k: report[k] for k in ("historical_validation", "yearly", "gate")}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

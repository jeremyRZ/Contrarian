from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.modules.xiaomi_directional import DirectionalParams, current_signal, evaluate, prepare


def clean(r: dict) -> dict:
    return {k: v for k, v in r.items() if k != "params"}


def acceptable(r: dict) -> bool:
    return (r["count"] >= 4 and r["sharpe"] > 0.25 and r["profit_factor"] > 1.03
            and r["max_drawdown_pct"] > -25)


def main() -> None:
    d = ROOT / "data" / "xiaomi_mean_reversion"
    x = prepare(pd.read_csv(d / "HK_01810_DAY.csv"), pd.read_csv(d / "HK_800700_DAY.csv"))
    dev = x[(x.time_key >= "2020-07-27") & (x.time_key < "2023-01-01")]
    val = x[(x.time_key >= "2023-01-01") & (x.time_key < "2025-01-01")]
    test = x[x.time_key >= "2025-01-01"]
    candidates = []
    for fast, slow in ((10, 60), (20, 80), (20, 120), (40, 120), (60, 200)):
        for threshold in (0.0, 0.02, 0.04):
            candidates.append(DirectionalParams("dual_ma", fast, slow, threshold))
    for slow in (20, 60, 120):
        for threshold in (0.0, 0.05, 0.10):
            candidates.append(DirectionalParams("momentum", 0, slow, threshold))
    for threshold in (2, 4, 6):
        candidates.append(DirectionalParams("ensemble", 0, 0, threshold))
    rows = []
    for p in candidates:
        a, b = evaluate(dev, p), evaluate(val, p)
        # Parameter selection is completed without inspecting the final test.
        score = min(a["sharpe"], b["sharpe"]) + min(a["annualized_pct"], b["annualized_pct"]) / 100
        if not (acceptable(a) and acceptable(b)):
            score = -999
        rows.append((score, p, a, b))
    score, selected, a, b = max(rows, key=lambda z: z[0])
    c = evaluate(test, selected)
    passed = bool(score > -999 and acceptable(c) and c["long_days"] > 20 and c["short_days"] > 20)
    result = {"data_range": [str(x.time_key.min().date()), str(x.time_key.max().date())],
              "selected": selected.__dict__, "development": clean(a), "validation": clean(b),
              "untouched_test": clean(c), "passed": passed,
              "current": current_signal(x, selected),
              "assumptions": {"next_open_execution": True, "allocation_pct": 30,
                              "fee_bps_each_position_change": 12,
                              "slippage_bps_each_position_change": 8,
                              "short_borrow_pct_annual": 8}}
    (d / "directional_result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()


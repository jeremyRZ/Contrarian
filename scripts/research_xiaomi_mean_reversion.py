from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.modules.xiaomi_mean_reversion import MeanReversionParams, evaluate, prepare


def summary(result: dict) -> dict:
    return {k: v for k, v in result.items() if k not in {"params", "trades"}}


def score(result: dict) -> float:
    if result["count"] < 12 or result["profit_factor"] < 1.0 or result["expectancy_pct"] <= 0:
        return -999.0
    return result["expectancy_pct"] + result["max_drawdown_pct"] / 100


def main() -> None:
    data_dir = ROOT / "data" / "xiaomi_mean_reversion"
    stock = pd.read_csv(data_dir / "HK_01810_DAY.csv")
    index = pd.read_csv(data_dir / "HK_800700_DAY.csv")
    x = prepare(stock, index)
    periods = {
        "development": x[(x.time_key >= "2020-07-27") & (x.time_key < "2023-01-01")],
        "validation": x[(x.time_key >= "2023-01-01") & (x.time_key < "2025-01-01")],
        "test": x[x.time_key >= "2025-01-01"],
    }
    report = {"data_range": [str(x.time_key.min().date()), str(x.time_key.max().date())],
              "costs": {"fee_bps_each_side": 12, "slippage_bps_each_side": 8,
                        "short_borrow_pct_annual": 8, "allocation_pct": 30},
              "directions": {}}
    for direction, regimes in (("long", ("any", "up")),
                               ("short", ("any", "down", "not_strong_up"))):
        candidates = []
        for rsi2 in (5, 10, 15, 20):
            for z20 in (1.0, 1.5, 2.0):
                for hold in (3, 5, 8, 10):
                    for stop in (5, 8, 10):
                        for regime in regimes:
                            p = MeanReversionParams(direction, rsi2, z20, hold, stop, regime)
                            dev = evaluate(periods["development"], p)
                            candidates.append((score(dev), p, dev))
        finalists = sorted(candidates, key=lambda z: z[0], reverse=True)[:12]
        ranked = []
        for _, p, dev in finalists:
            val = evaluate(periods["validation"], p)
            ranked.append((score(val), p, dev, val))
        _, selected, dev, val = max(ranked, key=lambda z: z[0])
        test = evaluate(periods["test"], selected)
        passed = (dev["count"] >= 12 and val["count"] >= 10 and test["count"] >= 10
                  and dev["profit_factor"] >= 1.15 and val["profit_factor"] >= 1.15
                  and test["profit_factor"] >= 1.15 and dev["expectancy_pct"] > 0
                  and val["expectancy_pct"] > 0 and test["expectancy_pct"] > 0)
        report["directions"][direction] = {
            "selected": selected.__dict__, "development": summary(dev),
            "validation": summary(val), "test": summary(test), "passed": bool(passed),
            "test_trades": test["trades"],
        }
    out = data_dir / "research_result.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

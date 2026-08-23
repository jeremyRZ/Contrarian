"""Walk-forward comparison of fixed SuperTrend overlays on HK strategies."""
from __future__ import annotations

import json
import sys
from dataclasses import asdict
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.run_walkforward_daily import (grid, load_data, metrics_from_folds,
                                            simulate)


MODES = ("baseline", "entry_confirmation", "exit_overlay", "hybrid")


def main() -> None:
    data, index, lots, _ = load_data()
    candidates = grid()
    years = range(2022, 2027)
    report = {"research_only": True, "universe_loaded": len(data),
              "supertrend": {"atr_period": 10, "multiplier": 3.0}, "modes": {}}
    mode_folds = {mode: [] for mode in MODES}
    mode_selections = {mode: [] for mode in MODES}
    for year in years:
        train_start = pd.Timestamp(f"{year - 3}-01-01")
        train_end = pd.Timestamp(f"{year - 1}-12-31")
        scored = []
        for params in candidates:
            train = simulate(data, index, lots, params, train_start, train_end,
                             supertrend_mode="baseline")
            selection_score = train["return_pct"] + .75 * train["max_dd_pct"]
            if (len(train["trades"]) < 12 or train["profit_factor"] < 1.05
                    or train["max_dd_pct"] < -15):
                selection_score = -999
            scored.append((selection_score, params, train))
        eligible = [row for row in scored if row[0] > -999]
        if eligible:
            _, params, train = max(eligible, key=lambda row: row[0])
            selected_params = asdict(params)
        else:
            params, selected_params = None, None
            train = {"return_pct": 0, "profit_factor": 0,
                     "max_dd_pct": 0, "trades": []}
        for mode in MODES:
            if params is None:
                test = {"ending": 20_000.0, "return_pct": 0,
                        "profit_factor": 0, "max_dd_pct": 0,
                        "trades": [], "curve": []}
            else:
                test = simulate(data, index, lots, params,
                                pd.Timestamp(f"{year}-01-01"),
                                pd.Timestamp(f"{year}-12-31"),
                                supertrend_mode=mode)
            mode_folds[mode].append(test)
            mode_selections[mode].append({
                "test_year": year, "params": selected_params,
                "train_return_pct": train["return_pct"],
                "test_return_pct": test["return_pct"],
                "test_profit_factor": test["profit_factor"],
                "test_max_drawdown_pct": test["max_dd_pct"],
                "test_trades": len(test["trades"]),
            })
        print("selected", year, selected_params, flush=True)

    for mode in MODES:
        folds, selections = mode_folds[mode], mode_selections[mode]
        metrics = metrics_from_folds(folds)
        report["modes"][mode] = {
            "folds": selections,
            "oos": {key: value for key, value in metrics.items() if key != "trades"},
            "distinct_stocks": len({trade["code"] for trade in metrics["trades"]}),
        }
        print(mode, report["modes"][mode]["oos"], flush=True)

    baseline = report["modes"]["baseline"]["oos"]
    for mode, result in report["modes"].items():
        oos = result["oos"]
        result["delta_vs_baseline"] = {
            "return_pct": oos["return_pct"] - baseline["return_pct"],
            "profit_factor": oos["profit_factor"] - baseline["profit_factor"],
            "max_drawdown_pct": oos["max_drawdown_pct"] - baseline["max_drawdown_pct"],
            "trade_count": oos["trade_count"] - baseline["trade_count"],
        }
    report["assumptions"] = {
        "selection": "rolling prior 3 years; next calendar year untouched",
        "comparison": "base parameters selected once without SuperTrend, then held fixed across overlays",
        "execution": "close signal, next-open fill",
        "capital_hkd": 20_000,
        "supertrend_parameters": "fixed before portfolio comparison; not fitted per stock",
        "costs": "minimum brokerage, platform fee, statutory levies, stamp duty, 8bps slippage per side",
    }
    output = ROOT / "data" / "supertrend_research" / "portfolio_supertrend_result.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()

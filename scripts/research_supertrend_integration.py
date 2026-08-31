"""Research SuperTrend integrations against the validated Xiaomi baseline."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.hk_costs import MODEL_ID
from app.modules.supertrend_research import (SuperTrendParams, combine_states,
                                             evaluate_positions, supertrend)
from app.modules.xiaomi_directional import DirectionalParams, desired_state, prepare


MODES = ("baseline", "standalone", "entry_confirmation", "exit_overlay", "hybrid")
SPLITS = {
    "development": ("2020-07-27", "2023-01-01"),
    "validation": ("2023-01-01", "2025-01-01"),
    "untouched_test": ("2025-01-01", None),
}


def slice_frame(frame: pd.DataFrame, start: str, end: str | None) -> pd.DataFrame:
    mask = frame.time_key >= start
    if end:
        mask &= frame.time_key < end
    return frame.loc[mask].copy()


def score(metrics: dict) -> float:
    if metrics["count"] < 4 or metrics["max_drawdown_pct"] <= -25:
        return -999.0
    return metrics["sharpe"] + metrics["annualized_pct"] / 100


def main() -> None:
    data_dir = ROOT / "data" / "xiaomi_mean_reversion"
    output_dir = ROOT / "data" / "supertrend_research"
    output_dir.mkdir(parents=True, exist_ok=True)
    frame = prepare(pd.read_csv(data_dir / "HK_01810_DAY.csv"),
                    pd.read_csv(data_dir / "HK_800700_DAY.csv"))
    baseline_params = DirectionalParams("momentum", 0, 20, 0.05)
    base = frame.apply(lambda row: desired_state(row, baseline_params), axis=1).astype(int)

    grid = [SuperTrendParams(period, multiplier)
            for period in (7, 10, 14, 20)
            for multiplier in (2.0, 2.5, 3.0, 3.5)]
    candidates: dict[str, list[dict]] = {mode: [] for mode in MODES if mode != "baseline"}
    baseline_results = {}
    for split_name, (start, end) in SPLITS.items():
        part = slice_frame(frame, start, end)
        baseline_results[split_name] = evaluate_positions(part, base.loc[part.index])

    # Select parameters using development and validation only. The final test is
    # evaluated after selection and never participates in ranking.
    for params in grid:
        st = supertrend(frame, params)["st_direction"]
        for mode in candidates:
            desired = combine_states(base, st, mode)
            dev = slice_frame(frame, *SPLITS["development"])
            val = slice_frame(frame, *SPLITS["validation"])
            dev_m = evaluate_positions(dev, desired.loc[dev.index])
            val_m = evaluate_positions(val, desired.loc[val.index])
            candidates[mode].append({
                "params": {"atr_period": params.atr_period, "multiplier": params.multiplier},
                "selection_score": min(score(dev_m), score(val_m)),
                "development": dev_m,
                "validation": val_m,
            })

    selected = {}
    for mode, rows in candidates.items():
        winner = max(rows, key=lambda row: row["selection_score"])
        params = SuperTrendParams(**winner["params"])
        st = supertrend(frame, params)["st_direction"]
        desired = combine_states(base, st, mode)
        test = slice_frame(frame, *SPLITS["untouched_test"])
        winner = {**winner, "untouched_test": evaluate_positions(test, desired.loc[test.index])}
        selected[mode] = winner

    baseline_test = baseline_results["untouched_test"]
    for mode, row in selected.items():
        test = row["untouched_test"]
        row["delta_vs_baseline_test"] = {
            "return_pct": test["return_pct"] - baseline_test["return_pct"],
            "sharpe": test["sharpe"] - baseline_test["sharpe"],
            "max_drawdown_pct": test["max_drawdown_pct"] - baseline_test["max_drawdown_pct"],
            "turnover_units": test["turnover_units"] - baseline_test["turnover_units"],
        }
        rows = candidates[mode]
        test_rows = []
        test_frame = slice_frame(frame, *SPLITS["untouched_test"])
        for candidate in rows:
            candidate_params = SuperTrendParams(**candidate["params"])
            candidate_trend = supertrend(frame, candidate_params)["st_direction"]
            candidate_state = combine_states(base, candidate_trend, mode)
            test_rows.append(evaluate_positions(
                test_frame, candidate_state.loc[test_frame.index]))
        row["parameter_stability"] = {
            "grid_size": len(rows),
            "positive_validation_share": sum(r["validation"]["return_pct"] > 0 for r in rows) / len(rows),
            "positive_validation_sharpe_share": sum(r["validation"]["sharpe"] > 0 for r in rows) / len(rows),
            "test_beats_baseline_return_share": sum(
                r["return_pct"] > baseline_test["return_pct"] for r in test_rows) / len(test_rows),
            "test_beats_baseline_sharpe_share": sum(
                r["sharpe"] > baseline_test["sharpe"] for r in test_rows) / len(test_rows),
            "median_test_return_delta_pct": float(pd.Series(
                [r["return_pct"] - baseline_test["return_pct"] for r in test_rows]).median()),
            "median_test_drawdown_delta_pct": float(pd.Series(
                [r["max_drawdown_pct"] - baseline_test["max_drawdown_pct"]
                 for r in test_rows]).median()),
        }

    result = {
        "research_only": True,
        "data_range": [str(frame.time_key.min().date()), str(frame.time_key.max().date())],
        "baseline": {"params": baseline_params.__dict__, **baseline_results},
        "selected_without_test_peeking": selected,
        "assumptions": {
            "signal_time": "daily close",
            "execution_time": "next trading day open",
            "allocation_pct": 30,
            "cost_model": MODEL_ID,
            "slippage_bps_each_position_change": 8,
            "short_borrow_pct_annual": 8,
            "supertrend_grid": {"atr_period": [7, 10, 14, 20],
                                "multiplier": [2.0, 2.5, 3.0, 3.5]},
        },
    }
    path = output_dir / "xiaomi_supertrend_result.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

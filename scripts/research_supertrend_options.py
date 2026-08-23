"""No-peeking SuperTrend plus HKEX-settlement Xiaomi option research."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.modules.xiaomi_convex_option_backtest import (build_supertrend_signals,
                                                        evaluate, metrics,
                                                        non_overlapping)
from app.modules.xiaomi_option_backtest import parse_dtop

PERIODS = {"development": ("2021-01-01", "2023-01-01"),
           "validation": ("2023-01-01", "2025-01-01"),
           "untouched_test": ("2025-01-01", "2099-01-01")}


def split_metrics(rows):
    return {name: metrics([row for row in rows if start <= row["entry_date"] < end])
            for name, (start, end) in PERIODS.items()}


def main() -> None:
    stock = pd.read_csv(ROOT / "data/xiaomi_mean_reversion/HK_01810_DAY.csv")
    stock["time_key"] = pd.to_datetime(stock.time_key)
    stock = stock[stock.time_key >= "2021-01-01"].sort_values("time_key").reset_index(drop=True)
    frames = {path.stem[-8:]: parse_dtop(path)
              for path in (ROOT / "data/xiaomi_option_backtest/dtop_miu").glob("MIU_*.rpt")}
    runs = []
    for mode in ("breakout_confirm", "breakout_recent_flip",
                 "momentum_confirm", "flip_with_momentum"):
        for atr_period in (7, 10, 14, 20):
            for multiplier in (2.0, 2.5, 3.0, 3.5):
                signals = build_supertrend_signals(
                    stock, mode=mode, atr_period=atr_period, multiplier=multiplier)
                for dte in ((15, 45), (30, 60)):
                    for otm_pct in (5, 10, 15):
                        for hold in (5, 10):
                            rows = [evaluate(signal, stock, frames, dte=dte,
                                             otm_pct=otm_pct, hold=hold)
                                    for signal in signals]
                            rows = non_overlapping([row for row in rows if row])
                            runs.append({
                                "params": {"mode": mode, "atr_period": atr_period,
                                           "multiplier": multiplier, "dte": dte,
                                           "otm_pct": otm_pct, "hold": hold},
                                "periods": split_metrics(rows), "trades": rows})

    def dev_score(run):
        dev = run["periods"]["development"]
        if dev.get("count", 0) < 8 or dev.get("profit_factor", 0) < 1.25:
            return -999
        return dev["portfolio_return_pct"] + dev["max_drawdown_pct"]

    ranked = sorted(runs, key=dev_score, reverse=True)
    selected = next((run for run in ranked if dev_score(run) > -999), ranked[0])
    p = selected["params"]
    signals = build_supertrend_signals(stock, mode=p["mode"], atr_period=p["atr_period"],
                                       multiplier=p["multiplier"])
    stress = {}
    for slippage in (5, 10, 15):
        rows = [evaluate(signal, stock, frames, dte=tuple(p["dte"]),
                         otm_pct=p["otm_pct"], hold=p["hold"],
                         slippage_pct=slippage, min_turnover=10, min_oi=100)
                for signal in signals]
        stress[str(slippage)] = split_metrics(non_overlapping([row for row in rows if row]))
    neighbors = [run for run in runs
                 if run["params"]["mode"] == p["mode"]
                 and run["params"]["dte"] == p["dte"]
                 and run["params"]["otm_pct"] == p["otm_pct"]
                 and run["params"]["hold"] == p["hold"]]
    val_positive = sum(run["periods"]["validation"].get("portfolio_return_pct", -1) > 0
                       for run in neighbors) / len(neighbors)
    test_positive = sum(run["periods"]["untouched_test"].get("portfolio_return_pct", -1) > 0
                        for run in neighbors) / len(neighbors)
    gate = {
        "validation_count_8": selected["periods"]["validation"].get("count", 0) >= 8,
        "validation_pf_1_25": selected["periods"]["validation"].get("profit_factor", 0) >= 1.25,
        "test_count_8": selected["periods"]["untouched_test"].get("count", 0) >= 8,
        "test_pf_1_25": selected["periods"]["untouched_test"].get("profit_factor", 0) >= 1.25,
        "stress_15pct_positive_all_periods": all(
            stress["15"][name].get("portfolio_return_pct", -1) > 0 for name in PERIODS),
        "neighbor_test_positive_75pct": test_positive >= .75,
    }
    gate["passed"] = all(gate.values())
    walk_folds, walk_rows, walk_stress_rows = [], [], []
    for year in range(2023, 2027):
        eligible = []
        for run in runs:
            training = non_overlapping([
                row for row in run["trades"] if row["entry_date"] < f"{year}-01-01"])
            train_metrics = metrics(training)
            if train_metrics.get("count", 0) < 5 or train_metrics.get("profit_factor", 0) < 1.25:
                continue
            score = train_metrics["portfolio_return_pct"] + train_metrics["max_drawdown_pct"]
            eligible.append((score, run, train_metrics))
        if not eligible:
            walk_folds.append({"year": year, "params": None, "test": {"count": 0}})
            continue
        _, winner, training_metrics = max(eligible, key=lambda item: item[0])
        year_rows = non_overlapping([
            row for row in winner["trades"]
            if f"{year}-01-01" <= row["entry_date"] < f"{year + 1}-01-01"])
        walk_rows.extend(year_rows)
        wp = winner["params"]
        year_signals = build_supertrend_signals(
            stock, mode=wp["mode"], atr_period=wp["atr_period"],
            multiplier=wp["multiplier"])
        stressed = [evaluate(signal, stock, frames, dte=tuple(wp["dte"]),
                             otm_pct=wp["otm_pct"], hold=wp["hold"],
                             slippage_pct=10, min_turnover=10, min_oi=100)
                    for signal in year_signals]
        stressed = non_overlapping([
            row for row in stressed if row and
            f"{year}-01-01" <= row["entry_date"] < f"{year + 1}-01-01"])
        walk_stress_rows.extend(stressed)
        walk_folds.append({"year": year, "params": wp,
                           "train": training_metrics, "test": metrics(year_rows)})
    walk_forward = {"folds": walk_folds, "combined": metrics(walk_rows),
                    "stress_10pct": metrics(walk_stress_rows)}
    walk_forward["passed"] = bool(
        walk_forward["combined"].get("count", 0) >= 12
        and walk_forward["combined"].get("profit_factor", 0) >= 1.25
        and walk_forward["combined"].get("portfolio_return_pct", -1) > 0
        and walk_forward["stress_10pct"].get("portfolio_return_pct", -1) > 0)
    report = {
        "method": {"source": "HKEX DTOP official settlements",
                   "signal": "underlying close; strike target fixed using signal close",
                   "entry": "next-session option settlement plus slippage",
                   "selection": "development only; validation and test excluded",
                   "risk_per_trade_pct": 1},
        "grid_count": len(runs), "selected": selected,
        "stress": stress,
        "parameter_stability": {"neighbors": len(neighbors),
                                "validation_positive_share": val_positive,
                                "test_positive_share": test_positive},
        "gate": gate,
        "walk_forward": walk_forward,
        "top_summaries": [{"params": run["params"], "periods": run["periods"]}
                          for run in ranked[:20]],
    }
    output = ROOT / "data/xiaomi_option_backtest/supertrend_option_results.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: report[key] for key in
                      ("grid_count", "selected", "stress", "parameter_stability", "gate", "walk_forward")},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

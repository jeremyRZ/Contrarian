from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.modules.xiaomi_convex_option_backtest import (
    build_convex_signals, evaluate, metrics, parameter_grid,
)
from app.modules.xiaomi_option_backtest import parse_dtop


def main() -> None:
    stock = pd.read_csv(ROOT / "data/xiaomi_mean_reversion/HK_01810_DAY.csv")
    stock["time_key"] = pd.to_datetime(stock.time_key)
    stock = stock[stock.time_key >= "2021-01-01"].sort_values("time_key").reset_index(drop=True)
    frames = {p.stem[-8:]: parse_dtop(p)
              for p in (ROOT / "data/xiaomi_option_backtest/dtop_miu").glob("MIU_*.rpt")}
    signals = build_convex_signals(stock)
    periods = {"development": ("2021-01-01", "2023-01-01"),
               "validation": ("2023-01-01", "2025-01-01"),
               "untouched_test": ("2025-01-01", "2099-01-01")}
    runs = []
    for params in parameter_grid():
        selected = signals if params["kind"] == "all" else [
            s for s in signals if s.kind == params["kind"]]
        rows = [evaluate(s, stock, frames, dte=params["dte"],
                         otm_pct=params["otm_pct"], hold=params["hold"])
                for s in selected]
        rows = [r for r in rows if r]
        split = {name: metrics([r for r in rows if start <= r["entry_date"] < end])
                 for name, (start, end) in periods.items()}
        runs.append({"params": params, "periods": split, "trades": rows})
    # Ranking is development-only. Validation and test are displayed, never
    # used to choose the winning parameter set.
    ranked = sorted(runs, key=lambda r: (
        r["periods"]["development"].get("profit_factor", 0),
        r["periods"]["development"].get("portfolio_return_pct", -999),
    ), reverse=True)
    robustness = []
    for candidate in ranked[:3]:
        params = candidate["params"]
        selected = [s for s in signals if params["kind"] == "all" or s.kind == params["kind"]]
        for slippage, min_turnover, min_oi in ((5, 10, 100), (10, 10, 100), (15, 10, 100)):
            rows = [evaluate(s, stock, frames, dte=params["dte"],
                             otm_pct=params["otm_pct"], hold=params["hold"],
                             slippage_pct=slippage, min_turnover=min_turnover,
                             min_oi=min_oi) for s in selected]
            rows = [r for r in rows if r]
            robustness.append({
                "params": params, "slippage_pct_per_side": slippage,
                "min_turnover": min_turnover, "min_oi": min_oi,
                "periods": {name: metrics([r for r in rows if start <= r["entry_date"] < end])
                            for name, (start, end) in periods.items()},
            })
    report = {
        "method": {"source": "HKEX DTOP official settlements",
                   "signal_timing": "close signal, next-session option entry",
                   "slippage_pct_per_side": 5, "risk_per_trade_pct": 1,
                   "selection": "development period only"},
        "signal_count": len(signals), "grid_count": len(runs),
        "top_development_ranked": ranked[:20],
        "robustness": robustness,
        "all_run_summaries": [{"params": r["params"], "periods": r["periods"]} for r in runs],
    }
    out = ROOT / "data/xiaomi_option_backtest/convex_results.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"signal_count": len(signals), "grid_count": len(runs),
                      "top": [{"params": r["params"], "periods": r["periods"]}
                              for r in ranked[:10]], "robustness": robustness},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

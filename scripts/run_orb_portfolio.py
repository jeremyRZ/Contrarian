"""Select one shared ORB parameter set across a portfolio of HK stocks."""
from __future__ import annotations

import json
import sys
from dataclasses import asdict, replace
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.modules.orb_strategy import OrbParams, backtest, prepare_bars

CACHE = ROOT / ".orb_cache"
OUT = ROOT / "orb_portfolio_results.json"
LOT = {"HK.00700": 100, "HK.09988": 100, "HK.03690": 100,
       "HK.01024": 100, "HK.00981": 500, "HK.01810": 200,
       "HK.01299": 200, "HK.02318": 500, "HK.09618": 50,
       "HK.01211": 100, "HK.01398": 1000, "HK.00388": 100}
SPREAD = {"HK.00700": 4.33, "HK.09988": 8.16, "HK.03690": 5.46,
          "HK.01024": 4.82, "HK.00981": 7.42, "HK.01810": 7.59,
          "HK.01299": 6.88, "HK.02318": 9.00, "HK.09618": 8.13,
          "HK.01211": 5.58, "HK.01398": 6.99, "HK.00388": 4.92}


def params_grid():
    base = OrbParams(max_range_bps=600, min_net_reward_risk=1.8,
                     allow_long=True, allow_short=False)
    for opening in (10, 15, 20):
        for confirm in (1, 2):
            for rr in (2.0, 2.5, 3.0):
                for last in ("11:30", "14:30"):
                    yield replace(base, opening_minutes=opening,
                                  confirm_bars=confirm, reward_risk=rr,
                                  last_entry_time=last)


def aggregate(reports):
    trades = [t for r in reports for t in r["trades"]]
    pnl = sum(t["pnl"] for t in trades)
    wins = sum(t["pnl"] for t in trades if t["pnl"] > 0)
    losses = -sum(t["pnl"] for t in trades if t["pnl"] < 0)
    rs = [t["r_multiple"] for t in trades]
    return {"trade_count": len(trades), "net_pnl": pnl,
            "win_rate": sum(t["pnl"] > 0 for t in trades) / len(trades) if trades else 0,
            "expectancy_r": sum(rs) / len(rs) if rs else 0,
            "profit_factor": wins / losses if losses else (999.0 if wins else 0.0)}


def main():
    frames = {}
    paths = list(CACHE.glob("HK_*_2025-08-01_2026-08-12.csv"))
    if not paths:  # backward-compatible short research cache
        paths = list(CACHE.glob("HK_*_2026-05-01_2026-08-12.csv"))
    for path in paths:
        code = path.name[:8].replace("_", ".")
        frames[code] = prepare_bars(pd.read_csv(path))
    if not frames:
        raise RuntimeError("no cached HK minute bars")
    dates = sorted(set().union(*(set(x.time_key.dt.date) for x in frames.values())))
    cut = dates[int(len(dates) * .7)]
    ranked = []
    for p in params_grid():
        train_reports, test_reports = [], []
        by_code = {}
        for code, frame in frames.items():
            train = frame[frame.time_key.dt.date < cut]
            test = frame[frame.time_key.dt.date >= cut]
            kw = {"lot_size": LOT[code], "spread_bps": SPREAD[code]}
            tr, te = backtest(train, p, **kw), backtest(test, p, **kw)
            train_reports.append(tr); test_reports.append(te)
            by_code[code] = {"train": {k: v for k, v in tr.items() if k != "trades"},
                             "test": {k: v for k, v in te.items() if k != "trades"}}
        train_agg, test_agg = aggregate(train_reports), aggregate(test_reports)
        # Select on training only. Low sample sizes are heavily discounted.
        score = train_agg["expectancy_r"] * min(1, train_agg["trade_count"] / 50)
        ranked.append({"score": score, "params": asdict(p), "train": train_agg,
                       "test": test_agg, "by_code": by_code})
    ranked.sort(key=lambda x: x["score"], reverse=True)
    result = {"split_date": str(cut), "codes": sorted(frames),
              "best_train_selected": ranked[0], "all_candidates": ranked}
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result["best_train_selected"], ensure_ascii=False))
    print("WROTE", OUT)


if __name__ == "__main__":
    main()

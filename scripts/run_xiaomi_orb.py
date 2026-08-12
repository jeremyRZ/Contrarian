"""Xiaomi-only ORB research for a HKD 20,000 account."""
from __future__ import annotations

import json
import sys
from dataclasses import asdict, replace
from pathlib import Path

import futu as ft
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.futu_client import build_client_from_config, load_config
from app.modules.orb_strategy import OrbParams, backtest, prepare_bars

CODE = "HK.01810"
EQUITY = 20_000.0
LOT_SIZE = 200
SPREAD_BPS = 7.59
CACHE = ROOT / ".orb_cache" / "HK_01810_2025-08-01_2026-08-12.csv"
OUT = ROOT / "xiaomi_orb_results.json"


def fetch():
    if CACHE.exists():
        return pd.read_csv(CACHE)
    client = build_client_from_config(load_config())
    ok, msg = client.connect()
    if not ok:
        raise RuntimeError(msg)
    pages, key = [], None
    try:
        while True:
            ret, data, key = client._quote.request_history_kline(
                CODE, start="2025-08-01", end="2026-08-12",
                ktype=ft.KLType.K_1M, max_count=1000, page_req_key=key)
            if ret != ft.RET_OK:
                raise RuntimeError(str(data))
            pages.append(data)
            if key is None:
                break
    finally:
        client.close()
    frame = pd.concat(pages, ignore_index=True)
    CACHE.parent.mkdir(exist_ok=True)
    frame.to_csv(CACHE, index=False)
    return frame


def grid():
    base = OrbParams(allow_long=True, allow_short=False, risk_per_trade=.01,
                     max_position_pct=1.0, max_range_bps=800,
                     min_net_reward_risk=1.5, fee_bps_per_side=12,
                     slippage_bps_per_side=5)
    for opening in (10, 15, 20):
        for buffer in (5, 10):
            for confirm in (1, 2):
                for rr in (2.0, 2.5, 3.0):
                    yield replace(base, opening_minutes=opening,
                                  buffer_bps=buffer,
                                  confirm_bars=confirm,
                                  reward_risk=rr,
                                  max_hold_bars=120,
                                  last_entry_time="14:30")


def compact(report):
    return {k: v for k, v in report.items() if k not in ("trades", "params")}


def main():
    frame = prepare_bars(fetch())
    dates = sorted(frame.time_key.dt.date.unique())
    train_end = dates[int(len(dates) * .60)]
    valid_end = dates[int(len(dates) * .80)]
    train = frame[frame.time_key.dt.date < train_end]
    valid = frame[(frame.time_key.dt.date >= train_end) &
                  (frame.time_key.dt.date < valid_end)]
    test = frame[frame.time_key.dt.date >= valid_end]
    kw = {"equity": EQUITY, "lot_size": LOT_SIZE,
          "spread_bps": SPREAD_BPS}
    ranked = []
    for p in grid():
        tr = backtest(train, p, **kw)
        va = backtest(valid, p, **kw)
        # Require evidence on both selection periods and penalise fragility.
        n = tr["trade_count"] + va["trade_count"]
        combined_expectancy = (tr["expectancy_r"] * tr["trade_count"] +
                               va["expectancy_r"] * va["trade_count"]) / n if n else -99
        consistency = min(tr["expectancy_r"], va["expectancy_r"])
        score = combined_expectancy * min(1, n / 30) + .5 * consistency
        if tr["trade_count"] < 5 or va["trade_count"] < 2:
            score -= 1
        ranked.append((score, p, tr, va))
    ranked.sort(key=lambda x: x[0], reverse=True)
    score, best, tr, va = ranked[0]
    te = backtest(test, best, **kw)
    full = backtest(frame, best, **kw)
    result = {
        "code": CODE, "capital_hkd": EQUITY, "lot_size": LOT_SIZE,
        "days": len(dates), "bars": len(frame),
        "train_end": str(train_end), "validation_end": str(valid_end),
        "selected_params": asdict(best), "selection_score": score,
        "train": compact(tr), "validation": compact(va),
        "untouched_test": compact(te), "full": compact(full),
        "test_trades": [{k: t[k] for k in ("trade_date", "entry", "stop",
                         "target", "exit", "exit_reason", "qty", "pnl",
                         "r_multiple")} for t in te["trades"]],
    }
    result["quality_gate"] = {
        "passed": bool(te["trade_count"] >= 30 and
                       te["expectancy_r"] > 0 and
                       te["profit_factor"] >= 1.2 and
                       te["max_drawdown_pct"] >= -8),
        "requirements": {"min_test_trades": 30, "min_expectancy_r": 0,
                         "min_profit_factor": 1.2,
                         "max_drawdown_pct_floor": -8},
        "deployment_mode": "paper" if (te["trade_count"] >= 30 and
                                          te["expectancy_r"] > 0 and
                                          te["profit_factor"] >= 1.2 and
                                          te["max_drawdown_pct"] >= -8)
                           else "disabled",
    }
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()

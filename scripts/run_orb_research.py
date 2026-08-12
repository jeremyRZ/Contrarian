"""Fetch HK minute bars from Futu OpenD and run reproducible ORB research."""
from __future__ import annotations

import itertools
import json
import os
import sys
from dataclasses import asdict, replace
from pathlib import Path

import futu as ft
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.futu_client import build_client_from_config, load_config
from app.modules.orb_strategy import OrbParams, backtest, walk_forward
from app.modules.orb_universe import HK_LIQUID_SEED, rank_hk_orb_candidates

OUT = ROOT / "orb_research_results.json"
CACHE = ROOT / ".orb_cache"
CACHE.mkdir(exist_ok=True)


def history(ctx, code, start="2025-08-01", end="2026-08-12"):
    path = CACHE / f"{code.replace('.', '_')}_{start}_{end}.csv"
    if path.exists():
        return pd.read_csv(path)
    pages, key = [], None
    while True:
        ret, data, key = ctx.request_history_kline(
            code, start=start, end=end, ktype=ft.KLType.K_1M,
            max_count=1000, page_req_key=key)
        if ret != ft.RET_OK:
            raise RuntimeError(f"{code}: {data}")
        pages.append(data)
        if key is None:
            break
    df = pd.concat(pages, ignore_index=True)
    df.to_csv(path, index=False)
    return df


def candidates():
    base = OrbParams(allow_short=False, max_range_bps=500,
                     min_net_reward_risk=2.0)
    # Compact, economically meaningful first pass.  Wider grids multiply
    # nearly identical variants and materially increase selection bias.
    for opening, buffer, confirm, atr, hold in itertools.product(
            (10, 15), (5, 10), (1, 2), (1.0, 1.2), (120,)):
        yield replace(base, opening_minutes=opening, buffer_bps=buffer,
                      confirm_bars=confirm, atr_multiplier=atr,
                      max_hold_bars=hold)


def main():
    client = build_client_from_config(load_config())
    ok, msg = client.connect()
    if not ok:
        raise RuntimeError(msg)
    snap, err = client.market_snapshot(HK_LIQUID_SEED)
    if err:
        raise RuntimeError(err)
    ranked = rank_hk_orb_candidates(snap, top_n=12)
    results = []
    grid = list(candidates())
    for _, row in ranked.iterrows():
        code = row.code
        bars = history(client._quote, code)
        common = {"lot_size": int(row.lot_size), "spread_bps": float(row.spread_bps)}
        wf = walk_forward(bars, grid, train_days=40, test_days=15, **common)
        full_rank = []
        for p in grid:
            rep = backtest(bars, p, **common)
            score = rep["expectancy_r"] * min(1, rep["trade_count"] / 30) + rep["max_drawdown_pct"] / 100
            full_rank.append((score, p, rep))
        _, best, full = max(full_rank, key=lambda x: x[0])
        item = {"code": code, "name": row.get("name", code),
                "lot_size": int(row.lot_size), "spread_bps": float(row.spread_bps),
                "bars": len(bars), "days": int(pd.to_datetime(bars.time_key).dt.date.nunique()),
                "best_params_full": asdict(best), "full": {k: v for k, v in full.items() if k != "trades"},
                "walk_forward": {k: v for k, v in wf.items() if k != "folds"}}
        results.append(item)
        print(json.dumps(item, ensure_ascii=False))
    results.sort(key=lambda x: (x["walk_forward"]["test_expectancy_r"],
                                x["walk_forward"]["test_profit_factor"]), reverse=True)
    OUT.write_text(json.dumps({"universe": ranked.code.tolist(), "results": results},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    client.close()
    print(f"WROTE {OUT}")


if __name__ == "__main__":
    main()

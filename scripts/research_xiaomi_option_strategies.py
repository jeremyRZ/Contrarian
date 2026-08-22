from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.modules.xiaomi_option_backtest import aggregate, build_trades, evaluate_trade, parse_dtop


def main() -> None:
    data = ROOT / "data" / "xiaomi_option_backtest" / "dtop_miu"
    frames = {p.stem[-8:]: parse_dtop(p) for p in data.glob("MIU_*.rpt")}
    stock = pd.read_csv(ROOT / "data" / "xiaomi_mean_reversion" / "HK_01810_DAY.csv")
    trades = build_trades(stock)
    families = ("single_atm", "single_otm5", "vertical10", "single_value_delta",
                "credit_vertical10")
    results = {family: [] for family in families}
    for trade in trades:
        entry = frames.get(trade.entry_date.replace("-", ""))
        exit_ = frames.get(trade.exit_date.replace("-", ""))
        if entry is None or exit_ is None:
            continue
        for family in families:
            result = evaluate_trade(trade, entry, exit_, family)
            if result:
                results[family].append(result)
    report = {"method": {"source": "HKEX DTOP official settlement prices",
                         "entry": "next trading day after completed 20-day momentum signal",
                         "exit": "state exit or 20 trading days", "slippage_pct_per_leg": 5,
                         "liquidity": "entry turnover >=10 and open interest >=100",
                         "portfolio_risk_per_trade_pct": 3}, "periods": {}}
    periods = {"development": ("2021-01-01", "2023-01-01"),
               "validation": ("2023-01-01", "2025-01-01"),
               "untouched_test": ("2025-01-01", "2099-01-01")}
    for name, (start, end) in periods.items():
        report["periods"][name] = {}
        for family, rows in results.items():
            subset = [r for r in rows if start <= r["entry_date"] < end]
            report["periods"][name][family] = aggregate(subset)
    report["trades"] = results
    out = ROOT / "data" / "xiaomi_option_backtest" / "results.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"method": report["method"], "periods": report["periods"]},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

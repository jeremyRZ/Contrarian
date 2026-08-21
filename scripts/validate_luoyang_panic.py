"""Cross-sectional validation for the locked CMOC panic-rebound rule."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import futu_client

UNIVERSE = {
    "SH.603993": "洛阳钼业", "SH.601899": "紫金矿业", "SH.600362": "江西铜业",
    "SZ.000878": "云南铜业", "SZ.000630": "铜陵有色", "SH.600111": "北方稀土",
    "SH.603799": "华友钴业", "SZ.300618": "寒锐钴业", "SH.601958": "金钼股份",
    "SH.600549": "厦门钨业", "SH.600392": "盛和资源",
}
START, END = "2014-01-01", "2026-08-13"
DROP_THRESHOLD = -0.04
MIN_VOLUME_RATIO = 1.25
HOLD_DAYS = 1
# Conservative round trip: 5 bps slippage each side, 3 bps commission each
# side, and 10 bps sell stamp duty. Minimum commission is immaterial here and
# is nevertheless applied against a CNY 100,000 reference order.
REFERENCE_NOTIONAL = 100_000.0


def fetch(code: str) -> pd.DataFrame:
    ft = futu_client.ft
    quote = ft.OpenQuoteContext("127.0.0.1", 11111)
    frames, page_key = [], None
    try:
        while True:
            ret, data, page_key = quote.request_history_kline(
                code, start=START, end=END, ktype=ft.KLType.K_DAY,
                autype=ft.AuType.QFQ, max_count=1000, page_req_key=page_key)
            if ret != ft.RET_OK:
                raise RuntimeError(f"{code}: {data}")
            frames.append(data)
            if page_key is None:
                break
    finally:
        quote.close()
    bars = pd.concat(frames, ignore_index=True).drop_duplicates("time_key")
    bars["time_key"] = pd.to_datetime(bars["time_key"])
    return bars.sort_values("time_key").reset_index(drop=True)


def trades_for(code: str, name: str, bars: pd.DataFrame) -> list[dict]:
    bars = bars.copy()
    bars["drop"] = bars["close"].pct_change()
    bars["volume_ratio"] = bars["volume"] / bars["volume"].rolling(20).mean()
    bars["ma200"] = bars["close"].rolling(200).mean()
    results, last_exit = [], -1
    for signal_i in range(20, len(bars) - HOLD_DAYS - 1):
        row = bars.iloc[signal_i]
        if signal_i <= last_exit:
            continue
        if (row["drop"] > DROP_THRESHOLD or row["volume_ratio"] < MIN_VOLUME_RATIO
                or row["close"] <= row["ma200"]):
            continue
        entry_i, exit_i = signal_i + 1, signal_i + HOLD_DAYS + 1
        entry = float(bars.iloc[entry_i]["open"]) * 1.0005
        exit_price = float(bars.iloc[exit_i]["open"]) * 0.9995
        gross = exit_price / entry - 1
        commission = 2 * max(5.0, REFERENCE_NOTIONAL * 0.0003) / REFERENCE_NOTIONAL
        net = gross - commission - 0.001
        results.append({
            "code": code, "name": name,
            "signal_date": str(row["time_key"].date()),
            "entry_date": str(bars.iloc[entry_i]["time_key"].date()),
            "exit_date": str(bars.iloc[exit_i]["time_key"].date()),
            "drop_pct": round(float(row["drop"]) * 100, 3),
            "volume_ratio": round(float(row["volume_ratio"]), 3),
            "entry": round(entry, 4), "exit": round(exit_price, 4),
            "net_return_pct": round(net * 100, 3), "win": net > 0,
        })
        last_exit = exit_i
    return results


def metrics(trades: list[dict]) -> dict:
    count = len(trades)
    wins = sum(bool(t["win"]) for t in trades)
    gains = sum(max(float(t["net_return_pct"]), 0) for t in trades)
    losses = abs(sum(min(float(t["net_return_pct"]), 0) for t in trades))
    return {
        "trades": count, "wins": wins,
        "win_rate_pct": round(wins / count * 100, 2) if count else 0,
        "average_net_return_pct": round(sum(t["net_return_pct"] for t in trades) / count, 3) if count else 0,
        "profit_factor": round(gains / losses, 3) if losses else None,
    }


def main() -> None:
    all_trades = []
    for code, name in UNIVERSE.items():
        all_trades.extend(trades_for(code, name, fetch(code)))
    report = {
        "rule": {"drop_threshold_pct": -4, "min_volume_ratio": 1.25,
                 "regime": "close_above_ma200",
                 "entry": "next_open", "exit": "fourth_open_after_signal",
                 "holding_sessions": HOLD_DAYS},
        "period": {"start": START, "end": END},
        "aggregate": metrics(all_trades),
        "by_stock": {code: metrics([t for t in all_trades if t["code"] == code]) for code in UNIVERSE},
        "by_period": {
            "2014_2021": metrics([t for t in all_trades if t["signal_date"] < "2022-01-01"]),
            "2022_2023": metrics([t for t in all_trades if "2022-01-01" <= t["signal_date"] < "2024-01-01"]),
            "2024_2026_oos": metrics([t for t in all_trades if t["signal_date"] >= "2024-01-01"]),
        },
        "trades": all_trades,
    }
    out = ROOT / ".runtime" / "luoyang_panic_validation.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("aggregate", "by_stock", "by_period")},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

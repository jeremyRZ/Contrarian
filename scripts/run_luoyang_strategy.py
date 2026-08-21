"""Print the latest completed-day signal for SH.603993 via FutuOpenD."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import futu_client
from app.modules.cn_luoyang_strategy import close_signal, latest_levels, prepare_bars


def fetch_daily() -> pd.DataFrame:
    ft = futu_client.ft
    quote = ft.OpenQuoteContext("127.0.0.1", 11111)
    frames, page_key = [], None
    try:
        while True:
            ret, data, page_key = quote.request_history_kline(
                "SH.603993", start="2012-01-01", end=str(date.today()),
                ktype=ft.KLType.K_DAY, autype=ft.AuType.QFQ,
                max_count=1000, page_req_key=page_key)
            if ret != ft.RET_OK:
                raise RuntimeError(str(data))
            frames.append(data)
            if page_key is None:
                break
    finally:
        quote.close()
    return pd.concat(frames, ignore_index=True).drop_duplicates("time_key")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--entry-price", type=float)
    parser.add_argument("--peak-price", type=float,
                        help="本次持仓以来最高价；持仓判断必须与成本价一起提供")
    args = parser.parse_args()
    holding = args.entry_price is not None or args.peak_price is not None
    if holding and (args.entry_price is None or args.peak_price is None):
        parser.error("持仓判断必须同时提供 --entry-price 和 --peak-price")

    raw = fetch_daily()
    bars = prepare_bars(raw)
    completed = bars[bars["time_key"].dt.date < date.today()]
    if completed.empty:
        raise RuntimeError("没有完整交易日日线")
    signal = close_signal(completed.iloc[-1], holding, args.entry_price, args.peak_price)
    print(json.dumps({"levels": latest_levels(completed), "signal": signal},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

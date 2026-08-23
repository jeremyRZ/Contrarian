"""Fetch an expanded current-investable HK research universe from Futu.

The output is isolated from the production 60-name pool.  It is intended to
increase independent backtest events; current-universe selection bias remains
explicit and must be handled by the research gate.
"""
from __future__ import annotations

import json
import sys
import time
from datetime import date, datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.futu_client import build_client_from_config, ft, load_config

OUT = ROOT / ".research_daily_150"
OUT.mkdir(exist_ok=True)


def fetch(quote, code: str, end: str) -> pd.DataFrame:
    pages, key = [], None
    while True:
        ret, frame, key = quote.request_history_kline(
            code, start="2018-01-01", end=end, ktype=ft.KLType.K_DAY,
            autype=ft.AuType.QFQ, max_count=1000, page_req_key=key)
        if ret != ft.RET_OK:
            raise RuntimeError(str(frame))
        pages.append(frame)
        if key is None:
            break
    return (pd.concat(pages, ignore_index=True).sort_values("time_key")
            .drop_duplicates("time_key", keep="last"))


def main() -> None:
    client = build_client_from_config(load_config())
    ok, error = client.connect()
    if not ok:
        raise RuntimeError(error)
    summary = {"generated_at": datetime.now().isoformat(timespec="seconds"),
               "selection": "current top 150 HK stocks by market value after price/lot/cap filters",
               "bias_warning": "not a point-in-time historical constituent universe",
               "ok": [], "failed": []}
    try:
        candidates, error = client.liquid_stock_candidates(
            min_price=2, max_lot_price=10_000, min_market_value=1_000_000_000, limit=150)
        if error or candidates is None or candidates.empty:
            raise RuntimeError(error or "empty candidate universe")
        basic, basic_error = client.stock_basicinfo()
        if basic_error or basic is None:
            raise RuntimeError(basic_error or "basic info unavailable")
        cols = [c for c in ("code", "lot_size") if c in basic.columns]
        universe = candidates.merge(basic[cols], on="code", how="left")
        universe = universe.dropna(subset=["lot_size"])
        universe.to_csv(OUT / "universe.csv", index=False)
        end = date.today().isoformat()
        codes = list(dict.fromkeys([*universe.code.astype(str), "HK.800000"]))
        request_window_start, request_count = time.monotonic(), 0
        for code in codes:
            path = OUT / f"{code.replace('.', '_')}.csv"
            try:
                if request_count >= 50:
                    elapsed = time.monotonic() - request_window_start
                    if elapsed < 31:
                        time.sleep(31 - elapsed)
                    request_window_start, request_count = time.monotonic(), 0
                frame = fetch(client._quote, code, end)
                request_count += max(1, (len(frame) + 999) // 1000)
                temporary = path.with_suffix(".csv.tmp")
                frame.to_csv(temporary, index=False)
                temporary.replace(path)
                summary["ok"].append({"code": code, "rows": len(frame),
                                      "first": str(frame.time_key.iloc[0]),
                                      "last": str(frame.time_key.iloc[-1])})
                print("OK", code, len(frame), flush=True)
            except Exception as exc:
                summary["failed"].append({"code": code, "error": str(exc)})
                print("FAILED", code, exc, flush=True)
    finally:
        client.close()
    (OUT / "fetch_meta.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"ok": len(summary["ok"]), "failed": len(summary["failed"])},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()

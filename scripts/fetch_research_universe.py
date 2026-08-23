"""Refresh the research universe with paginated, front-adjusted Futu bars."""
from __future__ import annotations

import json
import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.futu_client import build_client_from_config, ft, load_config

UNIVERSE = pd.read_csv(ROOT / ".universal_daily" / "research_universe_60.csv")
OUT = ROOT / ".universal_daily_60"
OUT.mkdir(exist_ok=True)


def fetch_all_pages(quote, code: str, start: str, end: str) -> pd.DataFrame:
    pages, page_key = [], None
    while True:
        ret, data, page_key = quote.request_history_kline(
            code, start=start, end=end, ktype=ft.KLType.K_DAY,
            autype=ft.AuType.QFQ, max_count=1000, page_req_key=page_key)
        if ret != ft.RET_OK:
            raise RuntimeError(str(data))
        pages.append(data)
        if page_key is None:
            break
    frame = pd.concat(pages, ignore_index=True)
    frame = frame.sort_values("time_key").drop_duplicates("time_key", keep="last")
    if frame.empty or frame.time_key.iloc[-1][:10] < end:
        # Weekends/holidays make exact end-date equality invalid; only reject a
        # cache that is more than seven calendar days stale.
        age = (pd.Timestamp(end) - pd.Timestamp(frame.time_key.iloc[-1])).days
        if age > 7:
            raise RuntimeError(f"latest bar is stale by {age} days")
    return frame


def main() -> None:
    client = build_client_from_config(load_config())
    ok, message = client.connect()
    if not ok:
        raise RuntimeError(message)
    end = date.today().isoformat()
    codes = list(dict.fromkeys([*UNIVERSE.code.astype(str), "HK.800000"]))
    summary = {"generated_at": datetime.now().isoformat(timespec="seconds"),
               "start": "2018-01-01", "end": end, "ok": [], "failed": []}
    try:
        for code in codes:
            path = OUT / f"{code.replace('.', '_')}.csv"
            temporary = path.with_suffix(".csv.tmp")
            try:
                frame = fetch_all_pages(client._quote, code, "2018-01-01", end)
                frame.to_csv(temporary, index=False)
                temporary.replace(path)
                item = {"code": code, "rows": len(frame),
                        "first": frame.time_key.iloc[0], "last": frame.time_key.iloc[-1]}
                summary["ok"].append(item)
                print("OK", item)
            except Exception as exc:  # preserve the previous cache on failure
                temporary.unlink(missing_ok=True)
                summary["failed"].append({"code": code, "error": str(exc)})
                print("FAILED", code, exc)
    finally:
        client.close()
    (OUT / "fetch_meta.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    if summary["failed"]:
        raise RuntimeError(f"{len(summary['failed'])} symbols failed; see fetch_meta.json")


if __name__ == "__main__":
    main()

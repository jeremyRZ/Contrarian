"""Read-only Futu collection into an independent research workspace."""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import shutil
import sqlite3
import time

import pandas as pd


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    if output == args.source.resolve() or args.source.resolve() in output.parents:
        raise ValueError("Use a workspace outside the production project")
    (output / ".universal_daily").mkdir(parents=True, exist_ok=True)
    daily = output / ".raw_daily"
    daily.mkdir(exist_ok=True)
    actions = output / ".corporate_actions"
    actions.mkdir(exist_ok=True)
    source = args.source / ".runtime/forward_ledger.sqlite3"
    with closing(sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)) as db:
        snapshots = db.execute("SELECT snapshot_date,code,payload FROM universe_snapshots").fetchall()
        temp_db = output / "forward_ledger.new.sqlite3"
        with closing(sqlite3.connect(temp_db)) as target:
            db.backup(target)
    temp_db.replace(output / "forward_ledger.sqlite3")
    for name in ("research_universe_60.csv", "research_universe_60.meta.json"):
        source_file = args.source / ".universal_daily" / name
        if source_file.exists():
            shutil.copy2(source_file, output / ".universal_daily" / name)
    current = pd.read_csv(output / ".universal_daily/research_universe_60.csv")
    codes = set(current.code) | {code for _, code, _ in snapshots} | {"HK.800000"}
    import futu as ft

    quote = ft.OpenQuoteContext(host="127.0.0.1", port=11111)
    results = []
    end = datetime.now(timezone(timedelta(hours=8))).date().isoformat()
    try:
        for code in sorted(codes):
            if code != "HK.800000":
                time.sleep(.7)
                ret, rehab = quote.get_rehab(code)
                if ret != ft.RET_OK:
                    results.append({"code": code, "error": "Corporate actions unavailable: " + str(rehab)[:200]})
                    continue
                rehab.to_csv(actions / (code.replace(".", "_") + ".csv"), index=False)
            pages, key = [], None
            while True:
                time.sleep(.7)
                ret, frame, key = quote.request_history_kline(
                    code, start="2018-01-01", end=end, ktype=ft.KLType.K_DAY,
                    autype=ft.AuType.NONE, max_count=1000, page_req_key=key)
                if ret != ft.RET_OK:
                    results.append({"code": code, "error": str(frame)[:300]})
                    break
                pages.append(frame)
                if key is None:
                    data = pd.concat(pages, ignore_index=True).sort_values("time_key").drop_duplicates("time_key")
                    path = daily / (code.replace(".", "_") + ".csv")
                    temporary = path.with_suffix(".new.csv")
                    data.to_csv(temporary, index=False)
                    temporary.replace(path)
                    results.append({"code": code, "rows": len(data),
                                    "first": str(data.time_key.iloc[0]), "last": str(data.time_key.iloc[-1])})
                    break
            print(json.dumps(results[-1], ensure_ascii=False), flush=True)
    finally:
        quote.close()
    (output / "history_fetch.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    if any("error" in row for row in results):
        raise RuntimeError("History collection incomplete; inspect history_fetch.json")


if __name__ == "__main__":
    main()

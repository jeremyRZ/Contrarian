"""Fetch HKEX DTOP dates needed by Xiaomi option backtests.

``--convex`` downloads the sparse entry/exit date set used by the breakout and
large-move study.  Only the MIU report is retained from each archive.
"""
from __future__ import annotations

import io
import argparse
import sys
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
OUT = ROOT / "data" / "xiaomi_option_backtest" / "dtop_miu"
URL = "https://www.hkex.com.hk/eng/stat/dmstat/oi/DTOP_O_{date}.zip"


def required_dates(convex: bool = False) -> list[str]:
    x = pd.read_csv(ROOT / "data" / "xiaomi_mean_reversion" / "HK_01810_DAY.csv")
    x["time_key"] = pd.to_datetime(x.time_key)
    x = x[x.time_key >= "2021-01-01"].sort_values("time_key").reset_index(drop=True)
    if convex:
        previous_close = x.close.shift(1)
        true_range = pd.concat([
            x.high - x.low,
            (x.high - previous_close).abs(),
            (x.low - previous_close).abs(),
        ], axis=1).max(axis=1)
        atr14 = true_range.rolling(14).mean()
        returns = x.close.pct_change()
        signal = (
            (x.close > x.high.shift(1).rolling(55).max())
            | (x.close < x.low.shift(1).rolling(55).min())
            | ((returns.abs() > .045) & (true_range / atr14 > 1.5))
        ).fillna(False)
        dates: set[str] = set()
        for i in x.index[signal]:
            # Signal is known after day i closes; earliest executable entry is i+1.
            for offset in (1, 2, 3, 5, 10):
                j = i + offset
                if j < len(x):
                    dates.add(x.loc[j, "time_key"].strftime("%Y%m%d"))
        return sorted(dates)
    x["mom20"] = x.close.pct_change(20)
    x["state"] = (x.mom20 > .05).astype(int) - (x.mom20 < -.05).astype(int)
    dates = set()
    for i in range(21, len(x) - 1):
        if x.loc[i, "state"] == 0 or x.loc[i, "state"] == x.loc[i - 1, "state"]:
            continue
        entry_i = i + 1
        exit_i = min(entry_i + 20, len(x) - 1)
        for j in range(entry_i, exit_i):
            if x.loc[j, "state"] != x.loc[i, "state"]:
                exit_i = j
                break
        dates.add(x.loc[entry_i, "time_key"].strftime("%Y%m%d"))
        dates.add(x.loc[exit_i, "time_key"].strftime("%Y%m%d"))
    return sorted(dates)


def fetch(date: str) -> tuple[str, str]:
    target = OUT / f"MIU_{date}.rpt"
    if target.exists():
        return date, "cached"
    session = requests.Session()
    session.mount("https://", HTTPAdapter(max_retries=Retry(
        total=3, connect=3, read=3, backoff_factor=.5,
        status_forcelist=(429, 500, 502, 503, 504),
    )))
    try:
        response = session.get(URL.format(date=date), timeout=30)
    except requests.RequestException:
        return date, "network_error"
    if response.status_code == 404:
        return date, "missing"
    response.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        matches = [n for n in archive.namelist() if n.lower().endswith("_opt_dtl_miu.rpt")]
        if not matches:
            return date, "no_miu"
        target.write_bytes(archive.read(matches[0]))
    return date, "downloaded"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--convex", action="store_true")
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    dates = required_dates(convex=args.convex)
    counts: dict[str, int] = {}
    # HKEX occasionally closes concurrent TLS connections; four workers are
    # materially more reliable while keeping the download bounded.
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(fetch, date) for date in dates]
        for future in as_completed(futures):
            date, status = future.result()
            counts[status] = counts.get(status, 0) + 1
            print(date, status, flush=True)
    print({"required": len(dates), **counts})


if __name__ == "__main__":
    main()

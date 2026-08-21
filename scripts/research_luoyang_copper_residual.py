"""Point-in-time walk-forward test: CMOC residual mean reversion vs copper.

Copper is shifted one month because a month's World Bank observation is not
assumed available before that month ends. Signals use month-end information and
orders execute at the next month's first open.
"""
from __future__ import annotations

import itertools
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
COST = 0.0026


@dataclass(frozen=True)
class Params:
    reg_window: int
    z_window: int
    entry_z: float
    exit_z: float
    copper_ma: int
    max_hold: int
    stop: float


def load_monthly() -> pd.DataFrame:
    d = pd.read_csv(ROOT / ".runtime" / "sh603993_qfq_daily.csv", parse_dates=["date"])
    d = d.sort_values("date").set_index("date")
    stock = d.resample("ME").agg(open=("open", "first"), close=("close", "last"))

    raw = pd.read_excel(ROOT / ".runtime" / "CMO-Historical-Data-Monthly.xlsx",
                        sheet_name="Monthly Prices", header=4)
    date_col = raw.columns[0]
    copper_col = next(c for c in raw.columns if str(c).strip().lower() == "copper")
    c = raw[[date_col, copper_col]].copy()
    c.columns = ["period", "copper"]
    c = c[c.period.astype(str).str.match(r"^\d{4}M\d{2}$")]
    c.index = pd.PeriodIndex(c.period.astype(str).str.replace("M", "-"), freq="M").to_timestamp("M")
    c.copper = pd.to_numeric(c.copper, errors="coerce")
    # Availability lag: at the Aug month-end signal, only Jul copper is used.
    stock["copper"] = c.copper.shift(1).reindex(stock.index)
    return stock.dropna().copy()


def features(x: pd.DataFrame, p: Params) -> pd.DataFrame:
    y = np.log(x.close)
    q = np.log(x.copper)
    cov = y.rolling(p.reg_window).cov(q)
    beta = cov / q.rolling(p.reg_window).var()
    alpha = y.rolling(p.reg_window).mean() - beta * q.rolling(p.reg_window).mean()
    resid = y - alpha - beta * q
    out = x.copy()
    out["z"] = (resid - resid.rolling(p.z_window).mean()) / resid.rolling(p.z_window).std()
    out["copper_ma"] = out.copper.rolling(p.copper_ma).mean()
    return out


def trades(x: pd.DataFrame, p: Params) -> pd.DataFrame:
    z = features(x, p)
    rows, pos = [], None
    # Signal at row i month-end, fill at row i+1 first open.
    for i in range(len(z) - 1):
        r, nxt = z.iloc[i], z.iloc[i + 1]
        if pos is None:
            if pd.notna(r.z) and r.z <= p.entry_z and r.copper > r.copper_ma:
                pos = {"entry_date": z.index[i + 1], "entry": float(nxt.open), "months": 0}
        else:
            pos["months"] += 1
            mark = float(r.close) / pos["entry"] - 1
            reason = None
            if mark <= -p.stop: reason = "stop"
            elif pd.notna(r.z) and r.z >= p.exit_z: reason = "mean_revert"
            elif pos["months"] >= p.max_hold: reason = "time"
            if reason:
                gross = float(nxt.open) / pos["entry"] - 1
                rows.append({**pos, "exit_date": z.index[i + 1], "exit": float(nxt.open),
                             "return": gross - COST, "reason": reason})
                pos = None
    return pd.DataFrame(rows)


def stats(t: pd.DataFrame) -> dict:
    if t.empty: return {"n": 0, "wr": 0, "avg": 0, "pf": 0, "ret": 0, "dd": 0}
    r = t["return"]
    eq = (1 + r).cumprod(); dd = eq / eq.cummax() - 1
    gains, losses = r[r > 0].sum(), -r[r <= 0].sum()
    return {"n": len(r), "wr": float((r > 0).mean()), "avg": float(r.mean()),
            "pf": float(gains / losses) if losses else math.inf,
            "ret": float(eq.iloc[-1] - 1), "dd": float(dd.min())}


def main() -> None:
    x = load_monthly()
    grid = [Params(*v) for v in itertools.product(
        (24, 36, 48, 60), (6, 9, 12, 18), (-1.0, -1.5, -2.0),
        (-0.25, 0.0, 0.5), (3, 6, 12), (3, 6, 9, 12), (0.10, 0.15, 0.20))]
    results = []
    for p in grid:
        t = trades(x, p)
        if t.empty: continue
        train = stats(t[t.entry_date < "2021-01-01"])
        val = stats(t[(t.entry_date >= "2021-01-01") & (t.entry_date < "2024-01-01")])
        # Pre-registered minimums; blind period is never used for selection.
        eligible = (train["n"] >= 5 and val["n"] >= 3 and
                    train["avg"] > 0 and val["avg"] > 0 and
                    train["pf"] >= 1.2 and val["pf"] >= 1.2 and
                    train["dd"] >= -0.25 and val["dd"] >= -0.25)
        if eligible:
            score = min(train["avg"], val["avg"]) + .01 * min(train["pf"], val["pf"])
            results.append((score, p, train, val, t))
    results.sort(key=lambda q: q[0], reverse=True)
    print(f"rows={len(x)} range={x.index.min().date()}..{x.index.max().date()} candidates={len(grid)} eligible={len(results)}")
    for score, p, tr, va, all_t in results[:10]:
        blind_t = all_t[all_t.entry_date >= "2024-01-01"]
        bl = stats(blind_t)
        passed = (bl["n"] >= 5 and bl["wr"] >= .55 and bl["avg"] > 0 and
                  bl["pf"] >= 1.3 and bl["dd"] >= -.25)
        print("PARAM", p, "TRAIN", tr, "VAL", va, "BLIND", bl, "PASS", passed)
        if passed:
            print(blind_t.to_string(index=False))


if __name__ == "__main__":
    main()

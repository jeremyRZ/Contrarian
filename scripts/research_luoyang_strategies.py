"""Walk-forward research of multiple SH.603993 daily strategy families.

Reads WeStock markdown K-lines from stdin. Candidate selection for every test
year uses only the preceding five calendar years.
"""
from __future__ import annotations

import itertools
import json
import math
import sys
from dataclasses import dataclass, asdict
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
COST = .0026  # 5bps slippage x2 + commission x2 + conservative sell stamp


def parse(text: str) -> pd.DataFrame:
    rows = []
    for line in text.splitlines():
        if line.startswith("| 20"):
            p = [v.strip() for v in line.strip().strip("|").split("|")]
            if len(p) >= 7:
                rows.append(p[:7])
    x = pd.DataFrame(rows, columns=["date", "open", "close", "high", "low", "volume", "amount"])
    x.date = pd.to_datetime(x.date)
    for c in ["open", "close", "high", "low", "volume"]:
        x[c] = pd.to_numeric(x[c])
    x = x.sort_values("date").drop_duplicates("date").reset_index(drop=True)
    delta = x.close.diff(); up = delta.clip(lower=0); down = -delta.clip(upper=0)
    for n in (2, 5, 14):
        rs = up.ewm(alpha=1/n, adjust=False).mean() / down.ewm(alpha=1/n, adjust=False).mean()
        x[f"rsi{n}"] = 100 - 100 / (1 + rs)
    for n in (5, 10, 20, 50, 100, 200):
        x[f"ma{n}"] = x.close.rolling(n).mean()
    for n in (20, 55, 120):
        x[f"high{n}"] = x.high.shift(1).rolling(n).max()
        x[f"low{n}"] = x.low.shift(1).rolling(n).min()
    x["ret1"] = x.close.pct_change(); x["ret5"] = x.close.pct_change(5)
    x["ret20"] = x.close.pct_change(20)
    x["vr"] = x.volume / x.volume.rolling(20).mean()
    x["ma200_slope"] = x.ma200 / x.ma200.shift(20) - 1
    return x


@dataclass(frozen=True)
class Candidate:
    family: str
    a: float
    b: float
    hold: int
    regime: int


def mask(x: pd.DataFrame, p: Candidate) -> pd.Series:
    uptrend = (x.close > x.ma200) & (x.ma50 > x.ma200) if p.regime == 2 else (x.close > x.ma200)
    if p.regime == 0:
        uptrend = pd.Series(True, index=x.index)
    if p.family == "panic":
        return (x.ret1 <= -p.a) & (x.vr >= p.b) & uptrend
    if p.family == "pullback":
        return (x.rsi2 <= p.a) & (x.close < x.ma10) & (x.ret20 > p.b) & uptrend
    if p.family == "breakout":
        level = x[f"high{int(p.a)}"]
        return (x.close > level) & (x.vr >= p.b) & uptrend & (x.ma200_slope > 0)
    if p.family == "reversal":
        return (x.ret5 <= -p.a) & (x.close > x.open) & (x.vr >= p.b) & uptrend
    raise ValueError(p.family)


def candidates() -> list[Candidate]:
    out = []
    out += [Candidate("panic", a, b, h, r) for a,b,h,r in itertools.product(
        (.03,.04,.05,.06,.07), (1,1.25,1.5,1.75), (1,2,3,5,10), (0,1,2))]
    out += [Candidate("pullback", a, b, h, r) for a,b,h,r in itertools.product(
        (5,10,15,20), (0,.05,.10), (2,3,5,10,15), (1,2))]
    out += [Candidate("breakout", a, b, h, r) for a,b,h,r in itertools.product(
        (20,55,120), (1,1.25,1.5), (10,20,40,60), (1,2))]
    out += [Candidate("reversal", a, b, h, r) for a,b,h,r in itertools.product(
        (.05,.08,.10,.12), (1,1.25,1.5), (2,3,5,10), (0,1,2))]
    return out


def trades(x: pd.DataFrame, p: Candidate, start: str, end: str) -> list[dict]:
    sig = mask(x, p).fillna(False).to_numpy(); a=pd.Timestamp(start); b=pd.Timestamp(end)
    out=[]; last=-1
    for i in np.flatnonzero(sig):
        if i <= last or i+p.hold+1 >= len(x) or not (a <= x.date.iloc[i] <= b):
            continue
        buy_i=i+1; sell_i=i+p.hold+1
        entry=float(x.open.iloc[buy_i])*1.0005; exit_price=float(x.open.iloc[sell_i])*.9995
        net=exit_price/entry-1-(COST-.001)
        out.append({"signal":str(x.date.iloc[i].date()),"buy_date":str(x.date.iloc[buy_i].date()),
                    "buy":round(entry,4),"sell_date":str(x.date.iloc[sell_i].date()),
                    "sell":round(exit_price,4),"net":net})
        last=sell_i
    return out


def metrics(ts: list[dict]) -> dict:
    r=[t["net"] for t in ts];n=len(r);w=sum(v>0 for v in r);g=sum(max(v,0) for v in r);l=abs(sum(min(v,0) for v in r))
    # Wilson lower bound discourages tiny, lucky samples.
    z=1.645
    lower=0 if not n else ((w/n+z*z/(2*n))-z*math.sqrt((w/n*(1-w/n)+z*z/(4*n))/n))/(1+z*z/n)
    return {"n":n,"wins":w,"win_rate":w/n if n else 0,"wilson90_lower":lower,
            "avg":float(np.mean(r)) if r else 0,"pf":g/l if l else (99 if g else 0)}


def main():
    x=parse(sys.stdin.read())
    if len(x)<1500:raise RuntimeError(f"需要完整历史日线，当前只有 {len(x)} 根")
    pool=candidates(); folds=[]; all_oos=[]
    for year in range(2019,2027):
        train_start=f"{year-5}-01-01"; train_end=f"{year-1}-12-31"
        ranked=[]
        for p in pool:
            m=metrics(trades(x,p,train_start,train_end))
            yearly = [metrics(trades(x, p, f"{y}-01-01", f"{y}-12-31"))
                      for y in range(year-5, year)]
            active = [z for z in yearly if z["n"] > 0]
            positive_year_rate = (sum(z["avg"] > 0 for z in active) / len(active)) if active else 0
            if (m["n"] >= 10 and m["pf"] >= 1.15 and m["avg"] > 0
                    and len(active) >= 3 and positive_year_rate >= .60):
                score = m["wilson90_lower"] + .20 * positive_year_rate + .03 * min(m["pf"], 3)
                m["positive_year_rate"] = positive_year_rate
                ranked.append((score,p,m))
        if not ranked:
            folds.append({"year":year,"selected":None,"test":metrics([])});continue
        _,p,tr=max(ranked,key=lambda z:z[0]);te=trades(x,p,f"{year}-01-01",f"{year}-12-31")
        all_oos.extend(te);folds.append({"year":year,"selected":asdict(p),"train":tr,"test":metrics(te),"trades":te})
    # Fixed-rule blind test: selection stops at 2023, final segment starts 2024.
    stable=[]
    for p in pool:
        a=metrics(trades(x,p,"2014-01-01","2020-12-31"));b=metrics(trades(x,p,"2021-01-01","2023-12-31"))
        if a["n"]>=12 and b["n"]>=6 and min(a["pf"],b["pf"])>=1.15 and min(a["avg"],b["avg"])>0:
            stable.append((min(a["wilson90_lower"],b["wilson90_lower"]),p,a,b))
    fixed=None
    if stable:
        _,p,a,b=max(stable,key=lambda z:z[0]);te=trades(x,p,"2024-01-01","2026-08-13")
        fixed={"params":asdict(p),"train":a,"validation":b,"blind":metrics(te),"trades":te}
    fold_tests=[f["test"] for f in folds if f.get("test",{}).get("n",0)>0]
    positive_test_year_rate=(sum(z["avg"]>0 for z in fold_tests)/len(fold_tests)) if fold_tests else 0
    wf_metrics=metrics(all_oos);wf_metrics["positive_test_year_rate"]=positive_test_year_rate
    wf_metrics["passed"]=(wf_metrics["n"]>=20 and wf_metrics["win_rate"]>=.58
                           and wf_metrics["pf"]>=1.3 and positive_test_year_rate>=.50)
    report={"bars":len(x),"range":[str(x.date.min().date()),str(x.date.max().date())],
            "walk_forward":{"aggregate":wf_metrics,"folds":folds},"fixed_blind":fixed}
    out=ROOT/".runtime"/"luoyang_strategy_research.json";out.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(report,ensure_ascii=False))

if __name__=="__main__":main()

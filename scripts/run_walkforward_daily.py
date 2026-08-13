from __future__ import annotations

"""Walk-forward research for small-account HK daily strategies.

The engine models one shared HKD 20,000 account, board lots, next-open fills,
minimum brokerage/platform charges, statutory levies, stamp duty and slippage.
It intentionally does not reuse the legacy six-strategy score.
"""
import itertools
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / ".universal_daily_60"
UNIVERSE = ROOT / ".universal_daily" / "research_universe_60.csv"


@dataclass(frozen=True)
class Params:
    family: str
    lookback: int
    threshold: float
    exit_ma: int
    max_hold: int
    stop_atr: float
    trailing_pct: float


def rsi(series: pd.Series, n: int = 2) -> pd.Series:
    d = series.diff(); gain = d.clip(lower=0); loss = -d.clip(upper=0)
    ag = gain.ewm(alpha=1/n, adjust=False, min_periods=n).mean()
    al = loss.ewm(alpha=1/n, adjust=False, min_periods=n).mean()
    return 100 - 100 / (1 + ag / al.replace(0, np.nan))


def load_data() -> tuple[dict[str, pd.DataFrame], pd.DataFrame, dict, dict]:
    u = pd.read_csv(UNIVERSE)
    lots = {str(x.code): int(x.lot_size) for _, x in u.iterrows()}
    names = {str(x.code): str(x["name"]) for _, x in u.iterrows()}
    out = {}
    for path in DATA.glob("HK_*.csv"):
        x = pd.read_csv(path); x.time_key = pd.to_datetime(x.time_key)
        x = x.sort_values("time_key").drop_duplicates("time_key", keep="last").set_index("time_key")
        code = str(x.code.iloc[-1])
        if code not in lots or len(x) < 260: continue
        c = x.close
        for n in (3, 5, 10, 20, 40, 50, 60, 120, 200): x[f"ma{n}"] = c.rolling(n).mean()
        x["rsi2"] = rsi(c, 2)
        prev = c.shift(); tr = pd.concat([(x.high-x.low), (x.high-prev).abs(), (x.low-prev).abs()], axis=1).max(axis=1)
        x["atr14"] = tr.rolling(14).mean()
        x["turn20"] = x.turnover.rolling(20).mean()
        x["vol_ratio"] = x.volume / x.volume.rolling(20).mean()
        x["ret3"] = c.pct_change(3)
        for n in (20, 60, 120): x[f"prior_high{n}"] = x.high.rolling(n).max().shift(1)
        out[code] = x
    idx = pd.read_csv(DATA / "HK_800000.csv"); idx.time_key = pd.to_datetime(idx.time_key)
    idx = idx.sort_values("time_key").drop_duplicates("time_key", keep="last").set_index("time_key")
    idx["ma120"] = idx.close.rolling(120).mean()
    return out, idx, lots, names


def order_cost(notional: float) -> float:
    # Conservative Futu HK small-order model: brokerage min HKD3 plus HKD15
    # platform fee, statutory levies/trading/settlement fee and stamp duty.
    brokerage = max(3.0, notional * .0003)
    platform = 15.0
    statutory = notional * (.000027 + .0000015 + .0000565 + .000042)
    stamp = math.ceil(notional * .001)
    return brokerage + platform + statutory + stamp


def entry_signal(x: pd.DataFrame, i: int, p: Params) -> tuple[bool, float]:
    z = x.iloc[i]
    liquid = z.turn20 >= 100_000_000 and z.close >= 2
    trend = z.close > z.ma200 and z.ma50 > z.ma200 and z.ma200 > x.ma200.iloc[max(0, i-20)]
    if not liquid or not trend: return False, -999.0
    if p.family == "rsi_pullback":
        ok = z.rsi2 < p.threshold and z.ret3 < -.02
        return bool(ok), float(-z.rsi2 + min(0, z.ret3) * -100)
    if p.family == "breakout":
        ok = z.close > z[f"prior_high{p.lookback}"] and z.vol_ratio >= p.threshold
        mom = z.close / x.close.iloc[max(0, i-p.lookback)] - 1
        return bool(ok), float(mom / max(.01, x.close.pct_change().iloc[max(0, i-60):i+1].std()))
    return False, -999.0


def simulate(data, index, lots, p: Params, start, end, capital=20_000., slippage_bps=8.0, omit=None,
             per_position_allocation=.15, family_override=None):
    omit = set(omit or []); dates = [d for d in index.index if start <= d <= end]
    cash = float(capital); pos = {}; pending_buy = []; pending_sell = set(); curve=[]; trades=[]
    slip = slippage_bps / 10_000
    for date in dates:
        # Orders generated at the prior close execute at today's open.
        for code in list(pending_sell):
            if code not in pos or date not in data[code].index: continue
            px = float(data[code].loc[date].open) * (1-slip); q=pos[code]["qty"]
            proceeds=q*px; fee=order_cost(proceeds); cash += proceeds-fee
            basis=pos[code]["basis"]; trades.append({"code":code,"entry":pos[code]["entry_date"],"exit":str(date.date()),"pnl":proceeds-fee-basis})
            del pos[code]
        pending_sell=set()
        value = cash + sum(v["qty"]*float(data[c].loc[date].close) for c,v in pos.items() if date in data[c].index)
        budget = value * per_position_allocation
        for _,code in sorted(pending_buy, reverse=True):
            if len(pos)>=4 or code in pos or code in omit or date not in data[code].index: continue
            px=float(data[code].loc[date].open)*(1+slip); lot=lots[code]; q=int(budget//(px*lot))*lot
            if not q: continue
            notional=q*px; fee=order_cost(notional)
            if notional+fee<=cash:
                cash-=notional+fee; pos[code]={"qty":q,"entry":px,"basis":notional+fee,"entry_date":str(date.date()),"bars":0,"peak":px,"atr":float(data[code].loc[date].atr14)}
        pending_buy=[]
        value = cash + sum(v["qty"]*float(data[c].loc[date].close) for c,v in pos.items() if date in data[c].index)
        curve.append((date,value))
        if date == dates[-1]: break
        market_ok = date in index.index and index.loc[date].close > index.loc[date].ma120
        candidates=[]
        for code,x in data.items():
            if code in omit or date not in x.index: continue
            i=x.index.get_loc(date)
            if i<220: continue
            if code in pos:
                h=pos[code]; h["bars"]+=1; h["peak"]=max(h["peak"],float(x.iloc[i].high)); z=x.iloc[i]
                stop=z.close <= h["entry"]-p.stop_atr*h["atr"]
                trail=p.trailing_pct>0 and z.close <= h["peak"]*(1-p.trailing_pct)
                if p.family=="rsi_pullback": exit_sig=z.close>z[f"ma{p.exit_ma}"] or z.rsi2>70
                else: exit_sig=z.close<z[f"ma{p.exit_ma}"]
                if stop or trail or exit_sig or h["bars"]>=p.max_hold or not market_ok: pending_sell.add(code)
            elif market_ok:
                ok,score=entry_signal(x,i,p)
                if family_override is None or p.family == family_override:
                    if ok: candidates.append((score,code))
        pending_buy=sorted(candidates,reverse=True)[:max(0,4-len(pos))]
    # Liquidate at final close so every fold has comparable realized P&L.
    last=dates[-1]
    for code,h in list(pos.items()):
        if last not in data[code].index: continue
        px=float(data[code].loc[last].close)*(1-slip); proceeds=h["qty"]*px; fee=order_cost(proceeds); cash+=proceeds-fee
        trades.append({"code":code,"entry":h["entry_date"],"exit":str(last.date()),"pnl":proceeds-fee-h["basis"]})
    a=np.array([v for _,v in curve],float); pnl=[t["pnl"] for t in trades]
    gains=sum(v for v in pnl if v>0); losses=-sum(v for v in pnl if v<0)
    return {"ending":cash,"return_pct":(cash/capital-1)*100,"max_dd_pct":float((a/np.maximum.accumulate(a)-1).min()*100) if len(a) else 0,
            "profit_factor":gains/losses if losses else (999 if gains else 0),"trades":trades,"curve":curve}


def grid():
    # Coarse, pre-declared grid.  A huge grid is both slow and a stronger
    # multiple-testing/overfitting machine; neighbourhood robustness follows.
    pull=[Params("rsi_pullback",0,r,e,h,2.0,0) for r,e,h in itertools.product((3,5),(3,5),(5,8))]
    brk=[Params("breakout",l,v,e,40,2.5,t) for l,v,e,t in itertools.product((20,60,120),(1.2,1.5),(10,20),(.10,.15))]
    return pull+brk


def metrics_from_folds(folds):
    capital=20_000.; all_trades=[]; values=[]
    for f in folds:
        scale=capital/20_000.; capital=f["ending"]*scale
        all_trades.extend([{**t,"pnl":t["pnl"]*scale} for t in f["trades"]])
        values.extend([v*scale for _,v in f["curve"]])
    pnl=[t["pnl"] for t in all_trades]; gain=sum(x for x in pnl if x>0); loss=-sum(x for x in pnl if x<0); a=np.array(values)
    return {"ending_hkd":capital,"return_pct":(capital/20_000-1)*100,"trade_count":len(pnl),"profit_factor":gain/loss if loss else (999 if gain else 0),
            "max_drawdown_pct":float((a/np.maximum.accumulate(a)-1).min()*100),"positive_year_rate":sum(f["return_pct"]>0 for f in folds)/len(folds),"trades":all_trades}


def main():
    data,index,lots,names=load_data(); candidates=grid(); years=range(2022,2027); folds=[]; selections=[]
    for year in years:
        train_start=pd.Timestamp(f"{year-3}-01-01"); train_end=pd.Timestamp(f"{year-1}-12-31")
        scored=[]
        for p in candidates:
            r=simulate(data,index,lots,p,train_start,train_end)
            score=r["return_pct"]+.75*r["max_dd_pct"]
            if len(r["trades"])<12 or r["profit_factor"]<1.05 or r["max_dd_pct"] < -15: score=-999
            scored.append((score,p,r))
        eligible=[z for z in scored if z[0] > -999]
        if eligible:
            score,p,tr=max(eligible,key=lambda z:z[0]); te=simulate(data,index,lots,p,pd.Timestamp(f"{year}-01-01"),pd.Timestamp(f"{year}-12-31"))
            params=asdict(p)
        else:
            p=None; tr={"return_pct":0,"profit_factor":0,"max_dd_pct":0,"trades":[]}
            te={"ending":20000.,"return_pct":0.,"max_dd_pct":0.,"profit_factor":0.,"trades":[],"curve":[]}; params=None
        folds.append(te); selections.append({"test_year":year,"params":params,"train":{"return_pct":tr["return_pct"],"pf":tr["profit_factor"],"dd":tr["max_dd_pct"],"n":len(tr["trades"])},"test":{"return_pct":te["return_pct"],"pf":te["profit_factor"],"dd":te["max_dd_pct"],"n":len(te["trades"])}})
        print(selections[-1],flush=True)
    base=metrics_from_folds(folds)
    traded=sorted({t["code"] for t in base["trades"]})
    # Stress the exact walk-forward selections, preserving each year's chosen params.
    stress=[]; loo=[]
    for year,s in zip(years,selections):
        if s["params"] is None:
            stress.append({"ending":20000.,"return_pct":0.,"max_dd_pct":0.,"profit_factor":0.,"trades":[],"curve":[]})
        else:
            p=Params(**s["params"]); stress.append(simulate(data,index,lots,p,pd.Timestamp(f"{year}-01-01"),pd.Timestamp(f"{year}-12-31"),slippage_bps=20))
    stress_m=metrics_from_folds(stress)
    for code in traded:
        fs=[]
        for year,s in zip(years,selections):
            if s["params"] is None:
                fs.append({"ending":20000.,"return_pct":0.,"max_dd_pct":0.,"profit_factor":0.,"trades":[],"curve":[]})
            else:
                fs.append(simulate(data,index,lots,Params(**s["params"]),pd.Timestamp(f"{year}-01-01"),pd.Timestamp(f"{year}-12-31"),omit=[code]))
        m=metrics_from_folds(fs); loo.append({"omitted":code,"name":names.get(code,code),**{k:m[k] for k in ("return_pct","profit_factor","max_drawdown_pct","trade_count")}})
    gate={"oos_trades_30":base["trade_count"]>=30,"pf_1_25":base["profit_factor"]>=1.25,"dd_under_15":base["max_drawdown_pct"]>=-15,
          "loo_all_positive":bool(loo) and min(x["return_pct"] for x in loo)>0,"stress_20bps_positive":stress_m["return_pct"]>0}
    gate["passed"]=all(gate.values())
    report={"model":"annual walk-forward; prior 3y train, next year test","capital_hkd":20000,"cost_model":{"commission":"0.03%, min HKD3/order","platform_hkd_order":15,"stamp":"0.1% rounded up/order","statutory_rate_side":.000127,"slippage_bps_side":8},"folds":selections,
            "oos":{k:v for k,v in base.items() if k!="trades"},"stress_20bps":{k:v for k,v in stress_m.items() if k!="trades"},"distinct_stocks":len(traded),"loo":loo,"gate":gate,"trades":base["trades"]}
    (ROOT/"walkforward_daily_results.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps({k:report[k] for k in ("oos","stress_20bps","distinct_stocks","gate")},ensure_ascii=False,indent=2))


if __name__=="__main__": main()

"""Research Xiaomi ORB with HSTECH, VWAP, volume and gap regimes."""
from __future__ import annotations

import json, sys
from dataclasses import asdict, replace
from pathlib import Path

import futu as ft
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.futu_client import build_client_from_config, load_config
from app.modules.orb_strategy import OrbParams, backtest, prepare_bars
from app.modules.xiaomi_regime import eligible_dates, filter_days

START, END = "2025-08-01", "2026-08-12"
CACHE = ROOT / ".orb_cache"
OUT = ROOT / "xiaomi_regime_orb_results.json"


def fetch(code):
    path = CACHE / f"{code.replace('.', '_')}_{START}_{END}.csv"
    if path.exists(): return pd.read_csv(path)
    c=build_client_from_config(load_config()); ok,msg=c.connect()
    if not ok: raise RuntimeError(msg)
    pages=[]; key=None
    try:
        while True:
            ret,data,key=c._quote.request_history_kline(code,start=START,end=END,
                ktype=ft.KLType.K_1M,max_count=1000,page_req_key=key)
            if ret != ft.RET_OK: raise RuntimeError(str(data))
            pages.append(data)
            if key is None: break
    finally: c.close()
    out=pd.concat(pages,ignore_index=True); out.to_csv(path,index=False); return out


def main():
    stock=prepare_bars(fetch("HK.01810")); index=prepare_bars(fetch("HK.800700"))
    dates=sorted(stock.time_key.dt.date.unique())
    train_end=pd.Timestamp("2026-03-13").date()
    valid_end=pd.Timestamp("2026-06-01").date()
    base=OrbParams(opening_minutes=10,buffer_bps=10,confirm_bars=2,
        max_range_bps=800,reward_risk=3,max_hold_bars=120,risk_per_trade=.01,
        max_position_pct=1,min_net_reward_risk=1.5,allow_long=True,allow_short=False,
        require_above_vwap=True,failed_breakout_exit=True)
    candidates=[]
    # Structural filters only; the old price parameters stay fixed.
    for volume_ratio in (.75,1.0,1.25):
      for gap in (1.5,3.0):
       for index_bps in (0,10,20):
        eligible=eligible_dates(stock,index,opening_minutes=10,
            volume_ratio=volume_ratio,max_abs_gap_pct=gap,
            min_index_open_return_bps=index_bps)
        filtered=filter_days(stock,eligible)
        tr=backtest(filtered[filtered.time_key.dt.date < train_end],base,
                    equity=20000,lot_size=200,spread_bps=7.59)
        va=backtest(filtered[(filtered.time_key.dt.date >= train_end)&
                            (filtered.time_key.dt.date < valid_end)],base,
                    equity=20000,lot_size=200,spread_bps=7.59)
        n=tr['trade_count']+va['trade_count']
        exp=(tr['expectancy_r']*tr['trade_count']+va['expectancy_r']*va['trade_count'])/n if n else -99
        score=exp*min(1,n/20)+.5*min(tr['expectancy_r'],va['expectancy_r'])
        if tr['trade_count']<5 or va['trade_count']<2: score-=1
        candidates.append((score,volume_ratio,gap,index_bps,eligible,tr,va))
    candidates.sort(key=lambda x:x[0],reverse=True)
    score,vr,gap,ib,eligible,tr,va=candidates[0]
    filtered=filter_days(stock,eligible)
    te=backtest(filtered[filtered.time_key.dt.date >= valid_end],base,
                equity=20000,lot_size=200,spread_bps=7.59)
    full=backtest(filtered,base,equity=20000,lot_size=200,spread_bps=7.59)
    compact=lambda r:{k:v for k,v in r.items() if k not in ('params','trades')}
    result={'code':'HK.01810','days':len(dates),'strategy':asdict(base),
      'regime':{'volume_ratio':vr,'max_abs_gap_pct':gap,'min_index_open_return_bps':ib},
      'eligible_days':len(eligible),'selection_score':score,'train':compact(tr),
      'validation':compact(va),'untouched_test':compact(te),'full':compact(full)}
    result['quality_gate']={'passed':te['trade_count']>=30 and te['expectancy_r']>0 and te['profit_factor']>=1.2}
    OUT.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False))

if __name__=='__main__': main()

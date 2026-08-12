from __future__ import annotations
import json,sys
from pathlib import Path
import numpy as np,pandas as pd
ROOT=Path(__file__).resolve().parents[1]

def simulate(x,fast,slow,stop_pct,allocation=.9,require_slope=False,trade_start=0):
 cash=20000.;qty=0;entry=0.;peak=0.;trades=[];curve=[]
 for i in range(max(slow+2,trade_start),len(x)-1):
  r,n=x.iloc[i],x.iloc[i+1];ma_fast=x.close.iloc[:i+1].rolling(fast).mean().iloc[-1];ma_slow=x.close.iloc[:i+1].rolling(slow).mean().iloc[-1]
  slow_prev=x.close.iloc[:i].rolling(slow).mean().iloc[-1]
  if qty==0 and r.close>ma_slow and ma_fast>ma_slow and (not require_slope or ma_slow>slow_prev):
   px=n.open*1.0008;qty=int((cash*allocation)//(px*200))*200
   if qty: cash-=qty*px*(1+.0012);entry=px;peak=px
  elif qty:
   peak=max(peak,r.high);exit_signal=r.close<ma_fast or r.close<peak*(1-stop_pct/100)
   if exit_signal:
    px=n.open*.9992;proceeds=qty*px*(1-.0012);pnl=proceeds-qty*entry*(1+.0012);cash+=proceeds;trades.append(pnl);qty=0
  curve.append(cash+(qty*r.close if qty else 0))
 if qty:
  px=x.iloc[-1].close*.9992;proceeds=qty*px*(1-.0012);pnl=proceeds-qty*entry*(1+.0012);cash+=proceeds;trades.append(pnl)
 peakv=np.maximum.accumulate(curve);dd=np.array(curve)/peakv-1 if curve else np.array([0])
 wins=sum(v for v in trades if v>0);loss=-sum(v for v in trades if v<0)
 return {'fast':fast,'slow':slow,'stop_pct':stop_pct,'allocation':allocation,
  'require_slope':require_slope,'trades':len(trades),'net':cash-20000,
  'return_pct':(cash/20000-1)*100,'pf':wins/loss if loss else (999 if wins else 0),
  'max_dd_pct':float(dd.min()*100),'win_rate':sum(v>0 for v in trades)/len(trades) if trades else 0}

def main():
 x=pd.read_csv(ROOT/'.orb_cache/HK_01810_DAY_2018_2026.csv');x.time_key=pd.to_datetime(x.time_key);x=x.sort_values('time_key').reset_index(drop=True)
 grid=[simulate(x,f,s,st,a,sl) for f in (20,40,60) for s in (60,120,200)
       if f<s for st in (15,20,25) for a in (.5,.6,.7,.8,.9) for sl in (False,True)]
 # Stability: rank on listing through 2023, report without re-selection on 2024+.
 split=x.time_key.searchsorted(pd.Timestamp('2024-01-01'));train=x.iloc[:split].reset_index(drop=True);test=x.iloc[max(0,split-200):].reset_index(drop=True)
 train_grid=[simulate(train,f,s,st,a,sl) for f in (20,40,60) for s in (60,120,200)
             if f<s for st in (15,20,25) for a in (.5,.6,.7,.8,.9) for sl in (False,True)]
 # Prefer return, but reject intolerable training drawdowns rather than hoping
 # the future will be kinder.
 feasible=[r for r in train_grid if r['max_dd_pct']>=-20 and r['trades']>=20]
 best=max(feasible,key=lambda r:r['return_pct']+.75*r['max_dd_pct'])
 te=simulate(test,best['fast'],best['slow'],best['stop_pct'],best['allocation'],best['require_slope'],trade_start=200)
 # Buy and hold over comparable test span.
 bh=(test.iloc[-1].close/test.iloc[200].open-1)*100
 test_live=test.iloc[200:].reset_index(drop=True)
 half_curve=10000+10000/test_live.iloc[0].open*test_live.close
 half_ret=(half_curve.iloc[-1]/20000-1)*100
 half_dd=float((half_curve/half_curve.cummax()-1).min()*100)
 result={'selected_on_pre_2024':best,'post_2024_test':te,
  'post_2024_buy_hold_pct':bh,
  'post_2024_half_buy_hold':{'return_pct':half_ret,'max_dd_pct':half_dd},
  'full_same_params':simulate(x,best['fast'],best['slow'],best['stop_pct'],best['allocation'],best['require_slope']),
  'top_full':sorted(grid,key=lambda r:r['return_pct']+.75*r['max_dd_pct'],reverse=True)[:10]}
 strategy_calmar=te['return_pct']/abs(te['max_dd_pct'])
 benchmark_calmar=half_ret/abs(half_dd)
 result['risk_adjusted']={'strategy_return_drawdown_ratio':strategy_calmar,
                          'half_buy_hold_return_drawdown_ratio':benchmark_calmar}
 result['gate']={'passed':bool(te['trades']>=30 and te['return_pct']>0 and
   te['max_dd_pct']>=-20 and te['pf']>=1.5 and
   strategy_calmar>=benchmark_calmar*1.5)}
 (ROOT/'long_trend_results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(result,ensure_ascii=False))
if __name__=='__main__':main()

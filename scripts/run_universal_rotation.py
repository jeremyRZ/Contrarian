from __future__ import annotations
import json
from pathlib import Path
import numpy as np,pandas as pd
ROOT=Path(__file__).resolve().parents[1];DATA=ROOT/'.universal_daily'
LOTS={'HK.00700':100,'HK.09988':100,'HK.03690':100,'HK.01024':100,'HK.01810':200,'HK.00941':500,'HK.01211':100,'HK.00981':500,'HK.09618':50,'HK.09888':50,'HK.00005':400,'HK.01299':200,'HK.02318':500,'HK.00388':100,'HK.00883':1000,'HK.02020':200,'HK.06618':50,'HK.09626':20,'HK.01398':1000,'HK.03988':1000}
def load():
 out={}
 for p in DATA.glob('HK_*.csv'):
  x=pd.read_csv(p);x.time_key=pd.to_datetime(x.time_key);x=x.sort_values('time_key').set_index('time_key');code=x.code.iloc[0]
  for n in (20,60,120):x[f'ma{n}']=x.close.rolling(n).mean()
  x['mom']=x.close.pct_change(60);x['turn20']=x.turnover.rolling(20).mean();x['volatility']=x.close.pct_change().rolling(60).std()*np.sqrt(252)
  out[code]=x
 return out
def simulate(data,mom_days,rebalance_days,slots,allocation,trade_start=None):
 dates=sorted(set().union(*(set(x.index) for x in data.values())));cash=20000.;pos={};events=[];curve=[]
 for di,date in enumerate(dates[:-1]):
  value=cash+sum(q*data[c].loc[date].close for c,q in pos.items() if date in data[c].index)
  curve.append(value)
  if di%rebalance_days or (trade_start is not None and date<trade_start):continue
  candidates=[]
  for code,x in data.items():
   if date not in x.index:continue
   loc=x.index.get_loc(date)
   if loc<max(120,mom_days):continue
   r=x.loc[date];mom=x.close.iloc[loc]/x.close.iloc[loc-mom_days]-1
   if r.close>r.ma60 and r.ma20>r.ma60 and r.turn20>=100_000_000 and r.volatility<.8:
    score=mom/r.volatility if r.volatility>0 else -99
    candidates.append((score,code,mom,r))
  selected=[c[1] for c in sorted(candidates,reverse=True)[:slots]]
  # Orders use each security's next available open.
  for code in list(pos):
   if code not in selected:
    x=data[code];loc=x.index.get_loc(date)
    if loc+1<len(x):
     px=x.iloc[loc+1].open*.9992;q=pos.pop(code);cash+=q*px*.9988
     events.append({'signal_date':str(date.date()),'trade_date':str(x.index[loc+1].date()),'side':'SELL','code':code,'price':px,'qty':q,'reason':'rank_or_trend_exit'})
  budget=value*allocation/max(1,len(selected))
  for code in selected:
   if code in pos:continue
   x=data[code];loc=x.index.get_loc(date)
   if loc+1>=len(x):continue
   px=x.iloc[loc+1].open*1.0008;lot=LOTS[code];q=int(budget//(px*lot))*lot
   cost=q*px*1.0012
   if q and cost<=cash:
    cash-=cost;pos[code]=q
    cand=next(c for c in candidates if c[1]==code)
    events.append({'signal_date':str(date.date()),'trade_date':str(x.index[loc+1].date()),'side':'BUY','code':code,'price':px,'qty':q,'reason':f'trend_ok; momentum={cand[2]:.2%}; score={cand[0]:.3f}'})
 arr=np.array(curve);dd=arr/np.maximum.accumulate(arr)-1
 return {'return_pct':(arr[-1]/20000-1)*100,'max_dd_pct':float(dd.min()*100),'events':events,'curve':curve,'dates':[str(d.date()) for d in dates[:len(curve)]]}
def main():
 d=load();pre={c:x[x.index<'2024-01-01'] for c,x in d.items()};grid=[]
 for mom in (40,60,90,120):
  for reb in (5,10,20):
   for slots in (1,2,3):
    for alloc in (.5,.7,.9):
     r=simulate(pre,mom,reb,slots,alloc);grid.append((r['return_pct']+.75*r['max_dd_pct'],mom,reb,slots,alloc,r))
 feasible=[z for z in grid if z[5]['max_dd_pct']>=-25]
 _,mom,reb,slots,alloc,tr=max(feasible,key=lambda z:z[0])
 # Full data with trading disabled until 2024 retains indicator warm-up.
 te=simulate(d,mom,reb,slots,alloc,pd.Timestamp('2024-01-01'))
 # Rebase curve at first 2024 observation.
 idx=next(i for i,v in enumerate(te['dates']) if v>='2024-01-01');base=te['curve'][idx];curve=np.array(te['curve'][idx:]);dd=curve/np.maximum.accumulate(curve)-1
 test={'return_pct':(curve[-1]/base-1)*100,'max_dd_pct':float(dd.min()*100),'events':[e for e in te['events'] if e['signal_date']>='2024-01-01']}
 # Leave-one-symbol-out robustness on the untouched period. Parameters stay
 # frozen; this tests whether one lucky constituent explains the result.
 loo=[]
 for omitted in sorted(d):
  subset={c:x for c,x in d.items() if c!=omitted}
  r=simulate(subset,mom,reb,slots,alloc,pd.Timestamp('2024-01-01'))
  j=next(i for i,v in enumerate(r['dates']) if v>='2024-01-01');a=np.array(r['curve'][j:]);base2=a[0]
  loo.append({'omitted':omitted,'return_pct':(a[-1]/base2-1)*100,
              'max_dd_pct':float((a/np.maximum.accumulate(a)-1).min()*100)})
 buys=[e for e in test['events'] if e['side']=='BUY']
 selected_counts={c:sum(e['code']==c for e in buys) for c in sorted(d)}
 result={'selection_reason':'Dynamic point-in-time liquidity + trend + volatility-adjusted momentum','pre_2024_selected':{'momentum_days':mom,'rebalance_days':reb,'slots':slots,'allocation':alloc,'result':{k:v for k,v in tr.items() if k not in ('events','curve','dates')}},'post_2024_test':test,
  'selected_counts':selected_counts,'leave_one_out':loo}
 result['gate']={'passed':bool(test['return_pct']>0 and test['max_dd_pct']>=-20 and
   len(test['events'])>=20 and min(z['return_pct'] for z in loo)>0 and
   max(z['max_dd_pct'] for z in loo)>=-20)}
 (ROOT/'universal_rotation_results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps({**result,'post_2024_test':{**test,'events_count':len(test['events']),'events':test['events'][-10:]}},ensure_ascii=False))
if __name__=='__main__':main()

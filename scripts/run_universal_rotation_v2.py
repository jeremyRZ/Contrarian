from __future__ import annotations
import json
from pathlib import Path
import numpy as np,pandas as pd
ROOT=Path(__file__).resolve().parents[1]
U=pd.read_csv(ROOT/'.universal_daily/research_universe_60.csv')
LOTS={r.code:int(r.lot_size) for _,r in U.iterrows()}
NAMES={r.code:r['name'] for _,r in U.iterrows()}

def load():
 out={}
 for p in (ROOT/'.universal_daily_60').glob('HK_*.csv'):
  x=pd.read_csv(p);x.time_key=pd.to_datetime(x.time_key);x=x.sort_values('time_key').set_index('time_key');code=x.code.iloc[0]
  if len(x)<250:continue
  x['ma20']=x.close.rolling(20).mean();x['ma60']=x.close.rolling(60).mean();x['ma120']=x.close.rolling(120).mean()
  x['turn20']=x.turnover.rolling(20).mean();x['vol60']=x.close.pct_change().rolling(60).std()*np.sqrt(252)
  out[code]=x
 return out

def simulate(data,mom_days,rebalance_days,slots,allocation,min_momentum,trade_start=None):
 dates=sorted(set().union(*(set(x.index) for x in data.values())));cash=20000.;pos={};events=[];curve=[]
 for di,date in enumerate(dates[:-1]):
  marked={c:q*data[c].loc[date].close for c,q in pos.items() if date in data[c].index}
  value=cash+sum(marked.values());curve.append((date,value))
  if di%rebalance_days or (trade_start is not None and date<trade_start):continue
  cand=[]
  for code,x in data.items():
   if date not in x.index:continue
   loc=x.index.get_loc(date)
   if loc<max(120,mom_days):continue
   r=x.loc[date];mom=r.close/x.close.iloc[loc-mom_days]-1
   # Absolute quality gate: ranking first is insufficient.
   if (r.close>r.ma60 and r.ma20>r.ma60 and r.ma60>r.ma120 and
       r.turn20>=100_000_000 and .12<=r.vol60<=.70 and mom>=min_momentum):
    score=mom/r.vol60;cand.append((score,code,mom,r.vol60))
  selected=[z[1] for z in sorted(cand,reverse=True)[:slots]]
  for code in list(pos):
   if code not in selected:
    x=data[code];loc=x.index.get_loc(date)
    if loc+1<len(x):
     px=x.iloc[loc+1].open*.9992;q=pos.pop(code);cash+=q*px*.9988
     events.append({'signal_date':str(date.date()),'trade_date':str(x.index[loc+1].date()),'side':'SELL','code':code,'name':NAMES.get(code,code),'price':px,'qty':q,'reason':'trend_or_rank_exit'})
  budget=value*allocation/slots
  for code in selected:
   if code in pos:continue
   x=data[code];loc=x.index.get_loc(date)
   if loc+1>=len(x):continue
   px=x.iloc[loc+1].open*1.0008;lot=LOTS[code];q=int(budget//(px*lot))*lot;cost=q*px*1.0012
   if q and cost<=cash:
    z=next(v for v in cand if v[1]==code);cash-=cost;pos[code]=q
    events.append({'signal_date':str(date.date()),'trade_date':str(x.index[loc+1].date()),'side':'BUY','code':code,'name':NAMES.get(code,code),'price':px,'qty':q,'reason':f'trend+liquidity; momentum={z[2]:.1%}; vol={z[3]:.1%}; score={z[0]:.2f}'})
 arr=np.array([v for _,v in curve]);dd=arr/np.maximum.accumulate(arr)-1
 return {'return_pct':(arr[-1]/20000-1)*100,'max_dd_pct':float(dd.min()*100),'events':events,'curve':curve}

def period_stats(r,start):
 c=[z for z in r['curve'] if z[0]>=start]
 if not c:return {'return_pct':0,'max_dd_pct':0}
 a=np.array([z[1] for z in c]);return {'return_pct':(a[-1]/a[0]-1)*100,'max_dd_pct':float((a/np.maximum.accumulate(a)-1).min()*100)}

def main():
 d=load();pre={c:x[x.index<'2024-01-01'] for c,x in d.items()}
 candidates=[]
 for mom in (60,90,120):
  for reb in (10,20):
   for slots in (2,3):
    for alloc in (.5,.7):
     for minimum in (0,.05,.10):
      r=simulate(pre,mom,reb,slots,alloc,minimum)
      # Require positive results in both broad pre-test regimes.
      p1=period_stats(r,pd.Timestamp('2019-01-01'));p2=period_stats(r,pd.Timestamp('2022-01-01'))
      stable=min(p1['return_pct'],p2['return_pct'])
      score=r['return_pct']+.75*r['max_dd_pct']+.5*stable
      if r['max_dd_pct']<-25 or stable<=0:score=-999
      candidates.append((score,mom,reb,slots,alloc,minimum,r,p1,p2))
 score,mom,reb,slots,alloc,minimum,tr,p1,p2=max(candidates,key=lambda z:z[0])
 te=simulate(d,mom,reb,slots,alloc,minimum,pd.Timestamp('2024-01-01'));test=period_stats(te,pd.Timestamp('2024-01-01'))
 events=[e for e in te['events'] if e['signal_date']>='2024-01-01'];buys=[e for e in events if e['side']=='BUY']
 counts={c:sum(e['code']==c for e in buys) for c in d};contributors=sum(v>0 for v in counts.values())
 # Remove each actually selected name while keeping parameters frozen.
 loo=[]
 for omitted in [c for c,v in counts.items() if v]:
  r=simulate({c:x for c,x in d.items() if c!=omitted},mom,reb,slots,alloc,minimum,pd.Timestamp('2024-01-01'))
  s=period_stats(r,pd.Timestamp('2024-01-01'));loo.append({'omitted':omitted,'name':NAMES.get(omitted,omitted),**s})
 result={'universe_size':len(d),'selection_logic':['20-day turnover >= HKD100m','close>MA60, MA20>MA60>MA120','annualized volatility 12%-70%','absolute momentum threshold','rank by momentum/volatility','hold top names or cash'],
  'selected_pre_2024':{'momentum_days':mom,'rebalance_days':reb,'slots':slots,'allocation':alloc,'min_momentum':minimum,'pre_result':{k:v for k,v in tr.items() if k not in ('events','curve')},'regime_2019_plus':p1,'regime_2022_plus':p2},
  'post_2024':{**test,'event_count':len(events),'buy_count':len(buys),'distinct_selected':contributors,'selection_counts':counts,'events':events},'leave_one_out':loo}
 result['gate']={'passed':bool(test['return_pct']>0 and test['max_dd_pct']>=-20 and len(buys)>=15 and contributors>=5 and min(z['return_pct'] for z in loo)>0)}
 (ROOT/'universal_rotation_v2_results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps({**result,'post_2024':{**result['post_2024'],'events':events[-12:]}},ensure_ascii=False))
if __name__=='__main__':main()

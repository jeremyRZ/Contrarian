from __future__ import annotations
import json
from pathlib import Path
import numpy as np,pandas as pd
ROOT=Path(__file__).resolve().parents[1];U=pd.read_csv(ROOT/'.universal_daily/research_universe_60.csv')
LOTS={r.code:int(r.lot_size) for _,r in U.iterrows()};NAMES={r.code:r['name'] for _,r in U.iterrows()}
def load():
 out={}
 for p in (ROOT/'.universal_daily_60').glob('HK_*.csv'):
  x=pd.read_csv(p);x.time_key=pd.to_datetime(x.time_key);x=x.sort_values('time_key').set_index('time_key');code=x.code.iloc[0]
  if code.startswith('HK.8') or len(x)<250:continue
  x['ma20']=x.close.rolling(20).mean();x['ma60']=x.close.rolling(60).mean();x['ma120']=x.close.rolling(120).mean();x['turn20']=x.turnover.rolling(20).mean();x['vol60']=x.close.pct_change().rolling(60).std()*np.sqrt(252)
  out[code]=x
 idx=pd.read_csv(ROOT/'.universal_daily_60/HK_800000.csv');idx.time_key=pd.to_datetime(idx.time_key);idx=idx.sort_values('time_key').set_index('time_key');idx['ma120']=idx.close.rolling(120).mean()
 return out,idx
def sim(data,index,mom_days,review,slots,alloc,minmom,market_on,trade_start=None):
 dates=sorted(index.index);cash=20000.;pos={};events=[];curve=[]
 for di,date in enumerate(dates[:-1]):
  value=cash+sum(q*data[c].loc[date].close for c,q in pos.items() if date in data[c].index);curve.append((date,value))
  if di%review or (trade_start is not None and date<trade_start):continue
  market_ok=(not market_on) or index.loc[date].close>index.loc[date].ma120
  eligible=[]
  for code,x in data.items():
   if date not in x.index:continue
   loc=x.index.get_loc(date)
   if loc<max(120,mom_days):continue
   r=x.loc[date];mom=r.close/x.close.iloc[loc-mom_days]-1
   trend=r.close>r.ma60 and r.ma20>r.ma60 and r.ma60>r.ma120
   liquid=r.turn20>=100_000_000 and .12<=r.vol60<=.70
   if market_ok and trend and liquid and mom>=minmom:eligible.append((mom/r.vol60,code,mom,r.vol60))
  eligible.sort(reverse=True);eligible_codes={z[1] for z in eligible}
  # Patient exit: sell only when absolute eligibility is lost, never merely
  # because another stock's rank moved slightly higher.
  for code in list(pos):
   if code not in eligible_codes:
    x=data[code];loc=x.index.get_loc(date)
    if loc+1<len(x):
     px=x.iloc[loc+1].open*.9992;q=pos.pop(code);cash+=q*px*.9988;events.append({'signal_date':str(date.date()),'trade_date':str(x.index[loc+1].date()),'side':'SELL','code':code,'name':NAMES.get(code,code),'price':px,'qty':q,'reason':'absolute_filter_failed'})
  openings=max(0,slots-len(pos));budget=value*alloc/slots
  for score,code,mom,vol in eligible:
   if openings<=0:break
   if code in pos:continue
   x=data[code];loc=x.index.get_loc(date)
   if loc+1>=len(x):continue
   px=x.iloc[loc+1].open*1.0008;lot=LOTS[code];q=int(budget//(px*lot))*lot;cost=q*px*1.0012
   if q and cost<=cash:
    cash-=cost;pos[code]=q;openings-=1;events.append({'signal_date':str(date.date()),'trade_date':str(x.index[loc+1].date()),'side':'BUY','code':code,'name':NAMES.get(code,code),'price':px,'qty':q,'reason':f'absolute filters; momentum={mom:.1%}; vol={vol:.1%}; score={score:.2f}'})
 a=np.array([v for _,v in curve]);return {'return_pct':(a[-1]/20000-1)*100,'max_dd_pct':float((a/np.maximum.accumulate(a)-1).min()*100),'curve':curve,'events':events}
def stats(r,start):
 a=np.array([v for d,v in r['curve'] if d>=start]);return {'return_pct':(a[-1]/a[0]-1)*100,'max_dd_pct':float((a/np.maximum.accumulate(a)-1).min()*100)} if len(a) else {'return_pct':0,'max_dd_pct':0}
def main():
 d,idx=load();pre={c:x[x.index<'2024-01-01'] for c,x in d.items()};pi=idx[idx.index<'2024-01-01'];grid=[]
 for mom in (60,90,120):
  for review in (5,10,20):
   for slots in (2,3):
    for alloc in (.5,.7,.9):
     for mm in (0,.05,.10):
      for mo in (False,True):
       r=sim(pre,pi,mom,review,slots,alloc,mm,mo);s19=stats(r,pd.Timestamp('2019-01-01'));s22=stats(r,pd.Timestamp('2022-01-01'))
       score=r['return_pct']+.75*r['max_dd_pct']+.5*min(s19['return_pct'],s22['return_pct'])
       if r['max_dd_pct']<-25 or min(s19['return_pct'],s22['return_pct'])<=0:score=-999
       grid.append((score,mom,review,slots,alloc,mm,mo,r,s19,s22))
 _,mom,review,slots,alloc,mm,mo,tr,s19,s22=max(grid,key=lambda z:z[0])
 te=sim(d,idx,mom,review,slots,alloc,mm,mo,pd.Timestamp('2024-01-01'));ts=stats(te,pd.Timestamp('2024-01-01'));events=[e for e in te['events'] if e['signal_date']>='2024-01-01'];buys=[e for e in events if e['side']=='BUY'];counts={c:sum(e['code']==c for e in buys) for c in d}
 loo=[]
 for c,v in counts.items():
  if not v:continue
  r=sim({k:x for k,x in d.items() if k!=c},idx,mom,review,slots,alloc,mm,mo,pd.Timestamp('2024-01-01'));loo.append({'omitted':c,'name':NAMES.get(c,c),**stats(r,pd.Timestamp('2024-01-01'))})
 result={'universe_size':len(d),'logic':['dynamic historical liquidity','absolute MA20>MA60>MA120 trend','absolute momentum and volatility limits','optional Hang Seng regime','patient hold until absolute filter fails','2-3 diversified holdings or cash'],'selected_pre_2024':{'momentum_days':mom,'review_days':review,'slots':slots,'allocation':alloc,'min_momentum':mm,'market_filter':mo,'result':{k:v for k,v in tr.items() if k not in ('curve','events')},'2019_plus':s19,'2022_plus':s22},'post_2024':{**ts,'events':events,'buy_count':len(buys),'distinct':sum(v>0 for v in counts.values()),'counts':counts},'loo':loo}
 result['gate']={'passed':bool(ts['return_pct']>0 and ts['max_dd_pct']>=-20 and len(buys)>=15 and result['post_2024']['distinct']>=5 and min(z['return_pct'] for z in loo)>0 and min(z['max_dd_pct'] for z in loo)>=-22)}
 (ROOT/'universal_rotation_v3_results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps({**result,'post_2024':{**result['post_2024'],'events':events[-12:]}},ensure_ascii=False))
if __name__=='__main__':main()

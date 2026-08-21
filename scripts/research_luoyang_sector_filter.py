"""CMOC strategy search with a pre-registered nonferrous-sector regime filter."""
from __future__ import annotations

import itertools, json, math, sys
from dataclasses import dataclass, asdict
from pathlib import Path
import numpy as np
import pandas as pd

ROOT=Path(__file__).resolve().parents[1]; COST=.0026

def parse(text):
 rows=[]
 for line in text.splitlines():
  if line.startswith('| 20'):
   p=[v.strip() for v in line.strip().strip('|').split('|')]
   if len(p)>=7:rows.append(p[:7])
 x=pd.DataFrame(rows,columns=['date','open','close','high','low','volume','amount']);x.date=pd.to_datetime(x.date)
 for c in ['open','close','high','low','volume']:x[c]=pd.to_numeric(x[c])
 return x.sort_values('date').drop_duplicates('date').reset_index(drop=True)

def prep(x,prefix=''):
 d=x.close.diff();u=d.clip(lower=0).ewm(alpha=.5,adjust=False).mean();v=(-d.clip(upper=0)).ewm(alpha=.5,adjust=False).mean()
 x=x.copy();x['rsi2']=100-100/(1+u/v);x['ret1']=x.close.pct_change();x['ret20']=x.close.pct_change(20);x['vr']=x.volume/x.volume.rolling(20).mean()
 for n in [10,20,50,200]:x[f'ma{n}']=x.close.rolling(n).mean()
 for n in [20,55,120]:x[f'hi{n}']=x.high.shift().rolling(n).max()
 return x

@dataclass(frozen=True)
class P: family:str;a:float;b:float;hold:int;sector_regime:int

def pool():
 q=[]
 q += [P('pullback',a,b,h,s) for a,b,h,s in itertools.product((5,10,15,20),(-.05,0,.05,.1),(2,3,5,10),(1,2))]
 q += [P('panic',a,b,h,s) for a,b,h,s in itertools.product((.03,.04,.05,.06),(1,1.25,1.5),(1,2,3,5),(1,2))]
 q += [P('breakout',a,b,h,s) for a,b,h,s in itertools.product((20,55,120),(1,1.25,1.5),(10,20,40),(1,2))]
 return q

def sig(x,p):
 sec=(x.sclose>x.sma200) if p.sector_regime==1 else ((x.sclose>x.sma200)&(x.sma50>x.sma200))
 own=(x.close>x.ma200)&(x.ma50>x.ma200)
 if p.family=='pullback':return own&sec&(x.rsi2<=p.a)&(x.close<x.ma10)&((x.ret20-x.sret20)>=p.b)
 if p.family=='panic':return own&sec&(x.ret1<=-p.a)&(x.vr>=p.b)
 return own&sec&(x.close>x[f'hi{int(p.a)}'])&(x.vr>=p.b)&((x.ret20-x.sret20)>0)

def trades(x,p,a,b):
 m=sig(x,p).fillna(False).to_numpy();a=pd.Timestamp(a);b=pd.Timestamp(b);o=[];last=-1
 for i in np.flatnonzero(m):
  if i<=last or i+p.hold+1>=len(x) or not(a<=x.date.iloc[i]<=b):continue
  en=x.open.iloc[i+1]*1.0005;ex=x.open.iloc[i+p.hold+1]*.9995;o.append(float(ex/en-1-(COST-.001)));last=i+p.hold+1
 return o

def met(r):
 n=len(r);w=sum(z>0 for z in r);g=sum(max(z,0) for z in r);l=abs(sum(min(z,0) for z in r));z=1.645
 low=0 if not n else ((w/n+z*z/(2*n))-z*math.sqrt((w/n*(1-w/n)+z*z/(4*n))/n))/(1+z*z/n)
 return dict(n=n,wins=w,win_rate=w/n if n else 0,wilson90=low,avg=float(np.mean(r)) if r else 0,pf=g/l if l else(99 if g else 0))

def main():
 text=sys.stdin.read();parts=text.split('===SECTOR===');
 if len(parts)!=2:raise RuntimeError('missing sector section')
 a=prep(parse(parts[0]));s=prep(parse(parts[1]));s=s[['date','close','ma50','ma200','ret20']].rename(columns={'close':'sclose','ma50':'sma50','ma200':'sma200','ret20':'sret20'})
 x=a.merge(s,on='date',how='inner');rank=[]
 for p in pool():
  m1=met(trades(x,p,'2018-01-01','2020-12-31'));m2=met(trades(x,p,'2021-01-01','2023-12-31'))
  if m1['n']>=5 and m2['n']>=5 and min(m1['pf'],m2['pf'])>=1.15 and min(m1['avg'],m2['avg'])>0:
   rank.append((min(m1['wilson90'],m2['wilson90']),p,m1,m2))
 rank.sort(reverse=True,key=lambda z:z[0]);best=None
 if rank:
  _,p,m1,m2=rank[0];blind=met(trades(x,p,'2024-01-01','2026-08-13'));best={'params':asdict(p),'train':m1,'validation':m2,'blind':blind}
 out={'bars':len(x),'candidates':len(pool()),'eligible_pre_blind':len(rank),'best':best};(ROOT/'.runtime/luoyang_sector_research.json').write_text(json.dumps(out,ensure_ascii=False,indent=2),encoding='utf8');print(json.dumps(out,ensure_ascii=False))
if __name__=='__main__':main()

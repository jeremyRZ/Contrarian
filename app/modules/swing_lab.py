"""Daily-bar, next-open swing strategy lab for HK equities."""
from __future__ import annotations
from dataclasses import asdict,dataclass
from math import floor
import numpy as np,pandas as pd

@dataclass(frozen=True)
class SwingParams:
 family:str; a:float; b:float; hold:int; stop_pct:float
 market_filter:bool=True

def prepare(stock,index):
 s=stock.copy();i=index.copy()
 for x in (s,i):x['time_key']=pd.to_datetime(x.time_key)
 i=i[['time_key','close']].rename(columns={'close':'idx_close'})
 # Preserve Xiaomi's full listing history. Index-dependent strategies simply
 # cannot signal before the HSTECH series begins; stock-only strategies can.
 x=s.merge(i,on='time_key',how='left').sort_values('time_key').reset_index(drop=True)
 for n in (5,10,20,60,120,200):x[f'ma{n}']=x.close.rolling(n).mean()
 x['idx_ma20']=x.idx_close.rolling(20).mean();x['idx_ma60']=x.idx_close.rolling(60).mean()
 delta=x.close.diff();up=delta.clip(lower=0);dn=-delta.clip(upper=0)
 for n in (2,5,14):x[f'rsi{n}']=100-100/(1+up.ewm(alpha=1/n,adjust=False).mean()/dn.ewm(alpha=1/n,adjust=False).mean())
 x['high20']=x.high.shift(1).rolling(20).max();x['low10']=x.low.shift(1).rolling(10).min()
 x['vol20']=x.volume.rolling(20).mean();x['ret5']=x.close.pct_change(5)*100
 stock_required = ["open", "high", "low", "close", "ma5", "ma10",
                   "ma20", "ma60", "ma120", "ma200", "rsi2", "rsi5", "rsi14",
                   "high20", "low10", "vol20", "ret5"]
 return x.dropna(subset=stock_required).reset_index(drop=True)

def signal(r,p):
 market=(r.idx_close>r.idx_ma60) if p.market_filter else True
 if p.family=='trend_pullback':return market and r.close>r.ma60 and r.close<=r.ma20*(1+p.a/100) and r.rsi5<=p.b
 if p.family=='oversold':return market and r.close<r.ma20*(1-p.a/100) and r.rsi2<=p.b
 if p.family=='oversold_uptrend':
  return r.close>r.ma120 and r.ma60>r.ma120 and r.close<r.ma20*(1-p.a/100) and r.rsi2<=p.b
 if p.family=='breakout':return market and r.close>r.high20*(1+p.a/100) and r.volume>=r.vol20*p.b
 if p.family=='relative_momentum':return market and r.close>r.ma20 and r.ret5>=p.a and r.idx_close>r.idx_ma20
 raise ValueError(p.family)

def evaluate(x,p,equity=20000,lot=200,fee_bps=12,slip_bps=8):
 cash=float(equity);trades=[];i=0
 while i<len(x)-1:
  if not signal(x.iloc[i],p):i+=1;continue
  entry=x.iloc[i+1].open*(1+slip_bps/10000);stop=entry*(1-p.stop_pct/100)
  risk_share=entry-stop+entry*(fee_bps*2+slip_bps*2)/10000
  qty=min(floor(cash*.02/risk_share/lot)*lot,floor(cash*.9/entry/lot)*lot)
  if qty<lot:i+=1;continue
  exit_i=min(i+1+p.hold,len(x)-1);exit_price=None;reason='max_hold'
  for j in range(i+1,exit_i+1):
   if x.iloc[j].low<=stop:exit_price=stop;exit_i=j;reason='stop';break
   # Trend-based exit, known only at close and filled conservatively at close.
   if p.family in ('trend_pullback','breakout','relative_momentum') and x.iloc[j].close<x.iloc[j].ma10:
    exit_price=x.iloc[j].close;exit_i=j;reason='ma10';break
  if exit_price is None:exit_price=x.iloc[exit_i].close
  exit_price*=1-slip_bps/10000
  pnl=(exit_price-entry)*qty-(entry+exit_price)*qty*fee_bps/10000
  risk=risk_share*qty;trades.append({'date':str(x.iloc[i+1].time_key.date()),'pnl':pnl,'r':pnl/risk,'qty':qty,'reason':reason})
  cash+=pnl;i=exit_i+1
 wins=sum(t['pnl'] for t in trades if t['pnl']>0);loss=-sum(t['pnl'] for t in trades if t['pnl']<0)
 rs=[t['r'] for t in trades];curve=equity+np.r_[0,np.cumsum([t['pnl'] for t in trades])];dd=curve/np.maximum.accumulate(curve)-1
 return {'params':asdict(p),'trades':trades,'count':len(trades),'net':sum(t['pnl'] for t in trades),
  'return_pct':sum(t['pnl'] for t in trades)/equity*100,'expectancy_r':np.mean(rs) if rs else 0,
  'pf':wins/loss if loss else (999 if wins else 0),'win_rate':sum(t['pnl']>0 for t in trades)/len(trades) if trades else 0,
  'max_dd_pct':float(dd.min()*100)}

from __future__ import annotations
import json,sys
from pathlib import Path
import pandas as pd
ROOT=Path(__file__).resolve().parents[1]; sys.path.insert(0,str(ROOT))
from app.modules.intraday_lab import LabParams,evaluate,features

DATA=ROOT/'.orb_cache/HK_01810_2025-08-01_2026-08-12.csv'
OUT=ROOT/'intraday_lab_results.json'

def grid():
 for dev in (50,80):
  for vol in (.75,1.0):
   for stop in (50,80): yield LabParams('vwap_reversion',dev,vol,stop,1.5,60)
 for spread in (5,15):
  for stop in (50,80):
   for rr in (1.5,2): yield LabParams('macd_trend',spread,0,stop,rr,90)
 for buf in (5,15):
  for stop in (50,80):
   for rr in (1.5,2): yield LabParams('failed_breakdown',buf,0,stop,rr,60)

def compact(r): return {k:v for k,v in r.items() if k not in ('params','trades')}
def main():
 x=features(pd.read_csv(DATA)); dates=sorted(x.time_key.dt.date.unique())
 # Six chronological walk-forward folds. Each candidate is selected only on
 # prior data, then applied to the next untouched block.
 block=35; folds=[]; oos=[]; candidates=list(grid())
 for end in range(105,len(dates)-block+1,block):
  train_dates=set(dates[max(0,end-105):end]); test_dates=set(dates[end:end+block])
  train=x[x.time_key.dt.date.isin(train_dates)]; test=x[x.time_key.dt.date.isin(test_dates)]
  ranked=[]
  for p in candidates:
   r=evaluate(train,p,equity=20000,lot_size=200)
   score=r['expectancy_r']*min(1,r['trade_count']/25)
   if r['trade_count']<8: score-=1
   ranked.append((score,p,r))
  score,p,tr=max(ranked,key=lambda z:z[0]); te=evaluate(test,p,equity=20000,lot_size=200)
  oos.extend(te['trades']); folds.append({'params':p.__dict__,'train':compact(tr),'test':compact(te)})
 wins=sum(t['pnl'] for t in oos if t['pnl']>0); losses=-sum(t['pnl'] for t in oos if t['pnl']<0)
 result={'days':len(dates),'folds':folds,'oos':{'trade_count':len(oos),
  'net_pnl':sum(t['pnl'] for t in oos),'expectancy_r':sum(t['r'] for t in oos)/len(oos) if oos else 0,
  'profit_factor':wins/losses if losses else (999 if wins else 0),
  'win_rate':sum(t['pnl']>0 for t in oos)/len(oos) if oos else 0}}
 result['quality_gate']={'passed':result['oos']['trade_count']>=30 and result['oos']['expectancy_r']>0 and result['oos']['profit_factor']>=1.2}
 OUT.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8'); print(json.dumps(result,ensure_ascii=False))
if __name__=='__main__':main()

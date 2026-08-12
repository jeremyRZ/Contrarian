from __future__ import annotations
import json,sys
from pathlib import Path
import pandas as pd
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from app.modules.swing_lab import SwingParams,evaluate,prepare

def grid():
 for mf in (True,False):
  for a in (0,2,4):
   for b in (20,30,40):
    for h in (5,10,20):yield SwingParams('trend_pullback',a,b,h,8,mf)
  for a in (3,5,8):
   for b in (5,10,15):
    for h in (3,5,8):yield SwingParams('oversold',a,b,h,8,mf)
  for a in (0,.5,1):
   for b in (1,1.5,2):
    for h in (10,20,40):yield SwingParams('breakout',a,b,h,10,mf)
  for a in (2,4,6):
   for h in (10,20,40):yield SwingParams('relative_momentum',a,0,h,10,mf)

def compact(r):return {k:v for k,v in r.items() if k not in ('params','trades')}
def main():
 s=pd.read_csv(ROOT/'.orb_cache/HK_01810_DAY_2018_2026.csv');ix=pd.read_csv(ROOT/'.orb_cache/HK_800700_DAY_2018_2026.csv');x=prepare(s,ix)
 dates=list(x.time_key);cands=list(grid());folds=[];oos=[]
 # Expanding 500-day training, 125-day unseen tests.
 for end in range(500,len(x)-124,125):
  train=x.iloc[:end];test=x.iloc[end:end+125];rank=[]
  for p in cands:
   r=evaluate(train,p)
   # A candidate must have enough observations; a two-trade winning streak is
   # not allowed to outrank a repeatable strategy.
   score=(r['expectancy_r']*min(1,r['count']/30)+r['max_dd_pct']/100
          if r['count']>=12 else -999)
   rank.append((score,p,r))
  _,p,tr=max(rank,key=lambda z:z[0]);te=evaluate(test,p);oos+=te['trades'];folds.append({'selected':p.__dict__,'train':compact(tr),'test':compact(te)})
 wins=sum(t['pnl'] for t in oos if t['pnl']>0);loss=-sum(t['pnl'] for t in oos if t['pnl']<0);curve=20000+pd.Series([0]+[t['pnl'] for t in oos]).cumsum();dd=curve/curve.cummax()-1
 agg={'count':len(oos),'net':sum(t['pnl'] for t in oos),'return_pct':sum(t['pnl'] for t in oos)/200,
  'expectancy_r':sum(t['r'] for t in oos)/len(oos) if oos else 0,'pf':wins/loss if loss else (999 if wins else 0),
  'win_rate':sum(t['pnl']>0 for t in oos)/len(oos) if oos else 0,'max_dd_pct':float(dd.min()*100)}
 result={'rows':len(x),'folds':folds,'oos':agg};result['gate']={'passed':agg['count']>=30 and agg['expectancy_r']>0 and agg['pf']>=1.2 and agg['max_dd_pct']>=-15}
 (ROOT/'swing_lab_results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(result,ensure_ascii=False))
if __name__=='__main__':main()

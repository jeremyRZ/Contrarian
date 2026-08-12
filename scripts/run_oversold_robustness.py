from __future__ import annotations
import json,sys
from pathlib import Path
import pandas as pd
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from app.modules.swing_lab import SwingParams,evaluate,prepare
def main():
 s=pd.read_csv(ROOT/'.orb_cache/HK_01810_DAY_2018_2026.csv');ix=pd.read_csv(ROOT/'.orb_cache/HK_800700_DAY_2018_2026.csv');x=prepare(s,ix)
 grid=[SwingParams('oversold',a,b,h,stop,False) for a in (0,1,2,3)
       for b in (5,10,15,20) for h in (2,3,4,5) for stop in (6,8,10)]
 folds=[];oos=[]
 for end in range(480,len(x)-59,60):
  train=x.iloc[max(0,end-480):end];test=x.iloc[end:end+60];rank=[]
  for p in grid:
   r=evaluate(train,p);score=r['expectancy_r']*min(1,r['count']/25)+r['max_dd_pct']/100 if r['count']>=12 else -999
   rank.append((score,p,r))
  _,p,tr=max(rank,key=lambda z:z[0]);te=evaluate(test,p);oos+=te['trades'];folds.append({'p':p.__dict__,'train':{k:v for k,v in tr.items() if k not in ('params','trades')},'test':{k:v for k,v in te.items() if k not in ('params','trades')}})
 wins=sum(t['pnl'] for t in oos if t['pnl']>0);loss=-sum(t['pnl'] for t in oos if t['pnl']<0)
 agg={'count':len(oos),'net':sum(t['pnl'] for t in oos),'return_pct':sum(t['pnl'] for t in oos)/200,'expectancy_r':sum(t['r'] for t in oos)/len(oos) if oos else 0,'pf':wins/loss if loss else (999 if wins else 0),'win_rate':sum(t['pnl']>0 for t in oos)/len(oos) if oos else 0}
 result={'folds':folds,'oos':agg,'gate':{'passed':bool(agg['count']>=30 and agg['expectancy_r']>0 and agg['pf']>=1.2)}}
 (ROOT/'oversold_robustness.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(result,ensure_ascii=False))
if __name__=='__main__':main()

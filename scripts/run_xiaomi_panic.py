from __future__ import annotations
import json,sys
from pathlib import Path
import pandas as pd
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from dataclasses import replace
from app.modules.xiaomi_panic import PanicParams,backtest

def main():
 stock=pd.read_csv(ROOT/'.orb_cache/HK_01810_2025-08-01_2026-08-12.csv')
 index=pd.read_csv(ROOT/'.orb_cache/HK_800700_2025-08-01_2026-08-12.csv')
 stock.time_key=pd.to_datetime(stock.time_key);index.time_key=pd.to_datetime(index.time_key)
 dates=sorted(stock.time_key.dt.date.unique());folds=[];all_trades=[]
 base=PanicParams()
 grid=[replace(base,stock_drop_pct=drop,relative_weakness_pct=weak,
               opening_volume_ratio=vol,confirmation_score=score)
       for drop in (-.6,-.9,-1.2) for weak in (.3,.5,.8)
       for vol in (.75,1.0,1.25) for score in (3,4)]
 # Rolling selection uses only the preceding 105 days.
 for end in range(105,len(dates)-34,35):
  train=set(dates[end-105:end]);test=set(dates[end:end+35])
  st=stock[stock.time_key.dt.date.isin(train)];it=index[index.time_key.dt.date.isin(train)]
  ranked=[]
  for p in grid:
   r=backtest(st,it,p,equity=20000,lot_size=200)
   score=r['expectancy_r']*min(1,r['trade_count']/15)
   if r['trade_count']<5:score-=1
   ranked.append((score,p,r))
  _,p,tr=max(ranked,key=lambda z:z[0])
  s=stock[stock.time_key.dt.date.isin(test)];ix=index[index.time_key.dt.date.isin(test)]
  r=backtest(s,ix,p,equity=20000,lot_size=200)
  folds.append({'selected':p.__dict__,'train':{k:v for k,v in tr.items() if k!='trades'},
                'test':{k:v for k,v in r.items() if k!='trades'}})
  all_trades.extend(r['trades'])
 wins=sum(t['pnl'] for t in all_trades if t['pnl']>0);loss=-sum(t['pnl'] for t in all_trades if t['pnl']<0)
 result={'folds':folds,'oos':{'trade_count':len(all_trades),'net_pnl':sum(t['pnl'] for t in all_trades),
  'expectancy_r':sum(t['r_multiple'] for t in all_trades)/len(all_trades) if all_trades else 0,
  'profit_factor':wins/loss if loss else (999 if wins else 0),'trades':all_trades}}
 result['quality_gate']={'passed':len(all_trades)>=20 and result['oos']['expectancy_r']>0 and result['oos']['profit_factor']>=1.2}
 (ROOT/'xiaomi_panic_results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
 print(json.dumps(result,ensure_ascii=False))
if __name__=='__main__':main()

from __future__ import annotations
import sys
from pathlib import Path
import futu as ft,pandas as pd
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from app.futu_client import build_client_from_config,load_config
U=pd.read_csv(ROOT/'.universal_daily/research_universe_60.csv')
OUT=ROOT/'.universal_daily_60';OUT.mkdir(exist_ok=True)
def main():
 c=build_client_from_config(load_config());ok,msg=c.connect()
 if not ok:raise RuntimeError(msg)
 try:
  for code in U.code:
   path=OUT/f'{code.replace(".","_")}.csv'
   if path.exists():continue
   pages=[];key=None
   while True:
    ret,d,key=c._quote.request_history_kline(code,start='2018-01-01',end='2026-08-12',ktype=ft.KLType.K_DAY,autype=ft.AuType.QFQ,max_count=1000,page_req_key=key)
    if ret!=ft.RET_OK:print('SKIP',code,d);break
    pages.append(d)
    if key is None:break
   if pages:
    x=pd.concat(pages,ignore_index=True);x.to_csv(path,index=False);print(code,len(x))
 finally:c.close()
if __name__=='__main__':main()

"""Cache WeStock markdown K-lines from stdin as a normalized CSV artifact."""
from pathlib import Path
import sys
import pandas as pd

rows=[]
for line in sys.stdin.read().splitlines():
    if line.startswith('| 20'):
        p=[v.strip() for v in line.strip().strip('|').split('|')]
        if len(p)>=7: rows.append(p[:7])
x=pd.DataFrame(rows,columns=['date','open','close','high','low','volume','amount'])
x['date']=pd.to_datetime(x['date'])
for c in ['open','close','high','low','volume','amount']:x[c]=pd.to_numeric(x[c])
x=x.sort_values('date').drop_duplicates('date')
out=Path(__file__).resolve().parents[1]/'.runtime'/'sh603993_qfq_daily.csv'
out.parent.mkdir(parents=True,exist_ok=True);x.to_csv(out,index=False);print(out,len(x))

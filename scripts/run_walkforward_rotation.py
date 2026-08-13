from __future__ import annotations

"""Annual walk-forward validation of a liquid HK trend-rotation strategy."""
import itertools, json, math, sys
from dataclasses import asdict, dataclass
from pathlib import Path
import numpy as np, pandas as pd

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from scripts.run_walkforward_daily import load_data, order_cost

@dataclass(frozen=True)
class P:
    momentum:int; review:int; slots:int; allocation:float; min_momentum:float

def run(data,index,lots,p,start,end,slip_bps=8,omit=None):
    omit=set(omit or []);dates=[d for d in index.index if start<=d<=end];cash=20000.;pos={};pending_sell=set();pending_buy=[];curve=[];trades=[];slip=slip_bps/10000
    for di,date in enumerate(dates):
        for code in list(pending_sell):
            if code not in pos or date not in data[code].index:continue
            px=float(data[code].loc[date].open)*(1-slip);q=pos[code]['q'];gross=q*px;fee=order_cost(gross);cash+=gross-fee
            trades.append({'code':code,'entry':pos[code]['date'],'exit':str(date.date()),'pnl':gross-fee-pos[code]['basis']});del pos[code]
        pending_sell=set()
        val=cash+sum(h['q']*float(data[c].loc[date].close) for c,h in pos.items() if date in data[c].index)
        budget=val*p.allocation/p.slots
        for _,code in sorted(pending_buy,reverse=True):
            if len(pos)>=p.slots or code in pos or code in omit or date not in data[code].index:continue
            px=float(data[code].loc[date].open)*(1+slip);lot=lots[code];q=int(budget//(px*lot))*lot
            if not q:continue
            gross=q*px;fee=order_cost(gross)
            if gross+fee<=cash:cash-=gross+fee;pos[code]={'q':q,'basis':gross+fee,'date':str(date.date())}
        pending_buy=[]
        val=cash+sum(h['q']*float(data[c].loc[date].close) for c,h in pos.items() if date in data[c].index);curve.append((date,val))
        if di%p.review or date==dates[-1]:continue
        market=index.loc[date].close>index.loc[date].ma120;eligible=[]
        for code,x in data.items():
            if code in omit or date not in x.index:continue
            i=x.index.get_loc(date)
            if i<max(200,p.momentum):continue
            z=x.iloc[i];mom=z.close/x.close.iloc[i-p.momentum]-1;vol=x.close.pct_change().rolling(60).std().iloc[i]*np.sqrt(252)
            ok=market and z.turn20>=1e8 and .12<=vol<=.70 and z.close>z.ma60 and z.ma20>z.ma60>z.ma120 and mom>=p.min_momentum
            if ok:eligible.append((mom/vol,code))
        elig={c for _,c in eligible}
        pending_sell={c for c in pos if c not in elig}
        pending_buy=sorted(eligible,reverse=True)[:max(0,p.slots-len(pos)+len(pending_sell))]
    last=dates[-1]
    for code,h in list(pos.items()):
        if last not in data[code].index:continue
        px=float(data[code].loc[last].close)*(1-slip);gross=h['q']*px;fee=order_cost(gross);cash+=gross-fee;trades.append({'code':code,'entry':h['date'],'exit':str(last.date()),'pnl':gross-fee-h['basis']})
    a=np.array([v for _,v in curve]);pn=[t['pnl'] for t in trades];g=sum(x for x in pn if x>0);l=-sum(x for x in pn if x<0)
    return {'ending':cash,'return_pct':(cash/20000-1)*100,'max_dd_pct':float((a/np.maximum.accumulate(a)-1).min()*100),'profit_factor':g/l if l else (999 if g else 0),'trades':trades,'curve':curve}

def combine(fs):
    cap=20000.;ts=[];vals=[]
    for f in fs:
        scale=cap/20000;cap=f['ending']*scale;ts += [{**t,'pnl':t['pnl']*scale} for t in f['trades']];vals += [v*scale for _,v in f['curve']]
    pn=[t['pnl'] for t in ts];g=sum(x for x in pn if x>0);l=-sum(x for x in pn if x<0);a=np.array(vals)
    return {'ending_hkd':cap,'return_pct':(cap/20000-1)*100,'trade_count':len(ts),'profit_factor':g/l if l else (999 if g else 0),'max_drawdown_pct':float((a/np.maximum.accumulate(a)-1).min()*100),'trades':ts}

def main():
    data,index,lots,names=load_data();grid=[P(*z) for z in itertools.product((60,90,120),(10,20),(3,4),(.4,.6),(.05,.10,.15))];years=range(2022,2027);sels=[];folds=[]
    for y in years:
        ss=[]
        for p in grid:
            r=run(data,index,lots,p,pd.Timestamp(f'{y-3}-01-01'),pd.Timestamp(f'{y-1}-12-31'))
            score=r['return_pct']+.8*r['max_dd_pct']
            if len(r['trades'])<8 or r['profit_factor']<1.15 or r['max_dd_pct']<-18:score=-999
            ss.append((score,p,r))
        valid=[z for z in ss if z[0]>-999]
        if not valid:p=None;tr=None;te={'ending':20000.,'return_pct':0.,'max_dd_pct':0.,'profit_factor':0.,'trades':[],'curve':[]}
        else:_,p,tr=max(valid,key=lambda z:z[0]);te=run(data,index,lots,p,pd.Timestamp(f'{y}-01-01'),pd.Timestamp(f'{y}-12-31'))
        sels.append({'year':y,'params':asdict(p) if p else None,'train':None if tr is None else {k:(len(tr['trades']) if k=='n' else tr[k]) for k in ('return_pct','profit_factor','max_dd_pct','n')},'test':{**{k:te[k] for k in ('return_pct','profit_factor','max_dd_pct')},'n':len(te['trades'])}});folds.append(te);print(sels[-1],flush=True)
    base=combine(folds);codes=sorted({t['code'] for t in base['trades']});stress=[]
    for y,s in zip(years,sels):stress.append(run(data,index,lots,P(**s['params']),pd.Timestamp(f'{y}-01-01'),pd.Timestamp(f'{y}-12-31'),20) if s['params'] else {'ending':20000.,'trades':[],'curve':[]})
    sm=combine(stress);loo=[]
    for code in codes:
        fs=[run(data,index,lots,P(**s['params']),pd.Timestamp(f'{y}-01-01'),pd.Timestamp(f'{y}-12-31'),omit=[code]) if s['params'] else {'ending':20000.,'trades':[],'curve':[]} for y,s in zip(years,sels)]
        m=combine(fs);loo.append({'omitted':code,'name':names.get(code,code),**{k:m[k] for k in ('return_pct','profit_factor','max_drawdown_pct','trade_count')}})
    gate={'oos_trades_30':base['trade_count']>=30,'pf_1_25':base['profit_factor']>=1.25,'dd_under_15':base['max_drawdown_pct']>=-15,'loo_all_positive':bool(loo) and min(x['return_pct'] for x in loo)>0,'stress_20bps_positive':sm['return_pct']>0};gate['passed']=all(gate.values())
    report={'capital_hkd':20000,'model':'annual walk-forward rotation','folds':sels,'oos':{k:v for k,v in base.items() if k!='trades'},'stress_20bps':{k:v for k,v in sm.items() if k!='trades'},'distinct_stocks':len(codes),'loo':loo,'gate':gate,'trades':base['trades']}
    (ROOT/'walkforward_rotation_results.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8');print(json.dumps({k:report[k] for k in ('oos','stress_20bps','distinct_stocks','gate')},ensure_ascii=False,indent=2))
if __name__=='__main__':main()

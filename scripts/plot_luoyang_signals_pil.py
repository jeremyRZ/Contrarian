"""Render CMOC candlesticks and locked-rule trades from WeStock markdown stdin."""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
from PIL import Image, ImageDraw, ImageFont


def font(size: int):
    path = Path("C:/Windows/Fonts/msyh.ttc")
    return ImageFont.truetype(str(path), size) if path.exists() else ImageFont.load_default()


def parse(text: str) -> pd.DataFrame:
    rows = []
    for line in text.splitlines():
        if line.startswith("| 20"):
            p = [v.strip() for v in line.strip().strip("|").split("|")]
            if len(p) >= 7:
                rows.append(p[:7])
    x = pd.DataFrame(rows, columns=["date", "open", "close", "high", "low", "volume", "amount"])
    x.date = pd.to_datetime(x.date)
    for c in ["open", "close", "high", "low", "volume"]:
        x[c] = pd.to_numeric(x[c])
    return x.sort_values("date").drop_duplicates("date").reset_index(drop=True)


def main():
    x = parse(sys.stdin.read())
    if len(x) < 220:
        raise RuntimeError(f"K线不足: {len(x)}")
    x["drop"] = x.close.pct_change()
    x["vr"] = x.volume / x.volume.rolling(20).mean()
    x["ma20"] = x.close.rolling(20).mean()
    x["ma200"] = x.close.rolling(200).mean()
    trades, last = [], -1
    for i in range(200, len(x) - 2):
        if (i > last and x["drop"].iloc[i] <= -.04 and x["vr"].iloc[i] >= 1.25
                and x["close"].iloc[i] > x["ma200"].iloc[i]):
            trades.append((i + 1, i + 2)); last = i + 2

    view = x.tail(170); first = int(view.index.min())
    W, H = 1800, 1000; L, R, T, B = 105, 55, 80, 180; price_bottom = 770
    im = Image.new("RGB", (W, H), "white"); d = ImageDraw.Draw(im)
    f_title, f_text, f_small = font(30), font(19), font(15)
    d.text((L, 22), "洛阳钼业 SH.603993｜恐慌反弹策略买卖点（日K）", fill="#172033", font=f_title)
    pmin, pmax = float(view.low.min()) * .95, float(view.high.max()) * 1.05
    vmax = float(view.volume.max())
    dx = (W-L-R) / max(len(view)-1, 1)
    yp = lambda p: T + (pmax-p)/(pmax-pmin)*(price_bottom-T)
    xv = lambda idx: L + (idx-first)*dx
    for k in range(6):
        p=pmin+(pmax-pmin)*k/5; y=yp(p); d.line((L,y,W-R,y),fill="#e6e9ef",width=1); d.text((10,y-10),f"{p:.1f}",fill="#667085",font=f_small)
    for idx,row in view.iterrows():
        xx=xv(idx); col="#d73a49" if row.close>=row.open else "#15965a"
        d.line((xx,yp(row.low),xx,yp(row.high)),fill=col,width=2)
        y1,y2=yp(max(row.open,row.close)),yp(min(row.open,row.close)); d.rectangle((xx-3,y1,xx+3,max(y2,y1+2)),fill=col)
        vh=row.volume/vmax*120; d.rectangle((xx-3,920-vh,xx+3,920),fill=col)
    for col,color in [("ma20","#8067dc"),("ma200","#e09f3e")]:
        pts=[(xv(i),yp(r[col])) for i,r in view.iterrows() if pd.notna(r[col])]
        if len(pts)>1:d.line(pts,fill=color,width=3)
    d.text((W-310,28),"MA20",fill="#8067dc",font=f_text);d.text((W-210,28),"MA200",fill="#e09f3e",font=f_text)
    shown=[]
    for bi,si in trades:
        if si<first:continue
        br,sr=x.iloc[bi],x.iloc[si];bx,by=xv(bi),yp(br.open);sx,sy=xv(si),yp(sr.open)
        d.polygon([(bx,by-16),(bx-11,by+7),(bx+11,by+7)],fill="#1769e0")
        d.text((bx-42,by+10),f"买 {br.open:.2f}",fill="#1769e0",font=f_small)
        d.polygon([(sx,sy+16),(sx-11,sy-7),(sx+11,sy-7)],fill="#f07c00")
        d.text((sx-42,sy-34),f"卖 {sr.open:.2f}",fill="#d76500",font=f_small)
        net=(sr.open*.9995)/(br.open*1.0005)-1-.0016;shown.append((br.date.date(),br.open,sr.date.date(),sr.open,net))
    for idx in range(first,len(x),21):
        xx=xv(idx);d.text((xx-38,935),x.date.iloc[idx].strftime("%Y-%m-%d"),fill="#667085",font=f_small)
    d.text((L,965),"规则：跌幅≥4%＋量比≥1.25＋收盘在MA200上方；次日开盘买，隔一交易日开盘卖",fill="#475467",font=f_text)
    out=Path(__file__).resolve().parents[1]/"luoyang_panic_signals.png";im.save(out)
    print(f"OUTPUT={out}")
    for z in shown:print(f"{z[0]} BUY {z[1]:.2f} -> {z[2]} SELL {z[3]:.2f} net={z[4]*100:.2f}%")

if __name__ == "__main__":main()

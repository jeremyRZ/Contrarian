"""
单票实时分析模块
- 快照（现价/涨跌/PE/PB/52周区间）
- K线技术面：MA5/10/20、RSI14、量能比
- 给技术面结论与操作建议
"""
from __future__ import annotations

from typing import Optional


def _num(v):
    try:
        if v is None or (isinstance(v, str) and v.strip().upper() in ("N/A", "NA", "")):
            return None
        f = float(v)
        return None if f != f else f  # NaN -> None
    except (ValueError, TypeError):
        return None


def _rsi(closes: list, period: int = 14) -> Optional[float]:
    if len(closes) <= period:
        return None
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(d, 0.0) for d in deltas]
    losses = [max(-d, 0.0) for d in deltas]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - 100 / (1 + rs), 1)


def _ma(closes: list, n: int) -> Optional[float]:
    if len(closes) < n:
        return None
    return round(sum(closes[-n:]) / n, 3)


def analyze(client, code: str):
    """单票实时分析。返回 (dict, error)。"""
    snap, err = client.market_snapshot([code])
    if err:
        return None, err
    if snap is None or snap.empty:
        return None, "无快照数据"
    cols = {c.lower(): c for c in snap.columns}
    row = snap.iloc[0]
    get = lambda k: _num(row[cols[k]]) if k in cols else None
    price = get("last_price")
    prev = get("prev_close_price")
    chg = get("change_rate")
    if chg is None and price and prev:
        chg = round((price - prev) / prev * 100, 2)
    pe = get("pe_ratio")
    pb = get("pb_ratio")
    hi = get("highest52weeks_price") or get("52_week_high")
    lo = get("lowest52weeks_price") or get("52_week_low")
    price = round(price, 3) if price is not None else None
    pe = round(pe, 2) if pe is not None else None
    pb = round(pb, 2) if pb is not None else None
    hi = round(hi, 3) if hi is not None else None
    lo = round(lo, 3) if lo is not None else None

    tech = {}
    signals = []
    k, kerr = client.history_kline(code, max_count=60)
    if k is not None and not k.empty and "close" in k.columns:
        closes = [_num(x) for x in k["close"].tolist()]
        closes = [c for c in closes if c is not None]
        ma5, ma10, ma20 = _ma(closes, 5), _ma(closes, 10), _ma(closes, 20)
        rsi = _rsi(closes, 14)
        vol = k["volume"].tolist() if "volume" in k.columns else []
        vol_ratio = None
        if len(vol) >= 6 and sum(vol[-6:-1]) > 0:
            vol_ratio = round(vol[-1] / (sum(vol[-6:-1]) / 5), 2)
        tech = {"ma5": ma5, "ma10": ma10, "ma20": ma20,
                "rsi14": rsi, "vol_ratio": vol_ratio}
        # 均线多空排列
        if ma5 and ma10 and ma20:
            if price and ma5 <= price and ma5 >= ma10 >= ma20:
                signals.append("均线多头排列")
            elif price and ma5 >= price and ma5 <= ma10 <= ma20:
                signals.append("均线空头排列")
        if rsi is not None:
            if rsi >= 70:
                signals.append(f"RSI超买({rsi})")
            elif rsi <= 30:
                signals.append(f"RSI超卖({rsi})")
        if vol_ratio and vol_ratio >= 2 and (chg or 0) < 1:
            signals.append(f"放量滞涨(量比{vol_ratio})")

    pos_pct = None
    if price and hi and lo and hi > lo:
        pos_pct = round((price - lo) / (hi - lo) * 100, 1)

    # 结论
    if pos_pct is not None and pos_pct < 25:
        conclusion = "处于52周低位，估值偏低"
    elif pos_pct is not None and pos_pct > 80:
        conclusion = "处于52周高位，注意追高风险"
    else:
        conclusion = "估值处于中部区间"
    if "RSI超买(70)" in " ".join(signals) or (tech.get("rsi14") or 0) >= 70:
        conclusion += "；技术面短期超买，警惕回落"
    elif (tech.get("rsi14") or 100) <= 30:
        conclusion += "；技术面短期超卖，关注反弹"

    return {
        "code": code,
        "name": str(row[cols["name"]]) if "name" in cols else code,
        "price": price,
        "change_rate": chg,
        "pe": pe,
        "pb": pb,
        "week52_high": hi,
        "week52_low": lo,
        "week52_position_pct": pos_pct,
        "technical": tech,
        "signals": signals,
        "conclusion": conclusion,
    }, None

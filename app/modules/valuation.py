"""
估值分析模块
- 优先用富途快照的 PE / PB / 52周区间
- 若用户提供财务字段（营收/eps/增速），计算 PS、PEG
- 输出估值分位、标签与结论（复用大北农研究框架的估值逻辑）
"""
from __future__ import annotations

from typing import Optional

import pandas as pd


def _get(df: pd.DataFrame, col: str, default=None):
    if df is None or df.empty:
        return default
    cols = {c.lower(): c for c in df.columns}
    key = col.lower()
    if key in cols:
        val = df.iloc[0][cols[key]]
        return val if pd.notna(val) else default
    return default


def _pe_label(pe):
    if pe is None:
        return "未知"
    if pe <= 0:
        return "亏损（PE无效）"
    if pe < 10:
        return "低估"
    if pe < 20:
        return "合理"
    if pe < 30:
        return "偏高"
    return "高估值"


def _pb_label(pb):
    if pb is None:
        return "未知"
    if pb < 1:
        return "破净（低）"
    if pb < 3:
        return "合理"
    if pb < 5:
        return "偏高"
    return "高估值"


def value_stock(client, code: str, financials: Optional[dict] = None):
    """
    对单只股票做估值分析。
    financials 可选字段: eps, bvps, revenue(总营收), growth_rate(净利润增速%, 用于PEG)
    返回 (result_dict, error)
    """
    snap, err = client.market_snapshot([code])
    if err:
        return None, err
    if snap is None or snap.empty:
        return None, f"未获取到 {code} 的行情快照"

    price = _get(snap, "last_price")
    pe = _get(snap, "pe_ratio")
    pb = _get(snap, "pb_ratio")
    # futu 快照真实列名：highest52weeks_price / lowest52weeks_price
    high52 = _get(snap, "highest52weeks_price") or _get(snap, "52_week_high")
    low52 = _get(snap, "lowest52weeks_price") or _get(snap, "52_week_low")
    mkt_cap = _get(snap, "total_market_val")
    name = _get(snap, "name", code)
    # 快照无 change_rate 列，用昨收价推导
    change_rate = _get(snap, "change_rate")
    prev_close = _get(snap, "prev_close_price")
    if change_rate is None and price is not None and prev_close:
        try:
            change_rate = round((price - prev_close) / prev_close * 100, 2)
        except Exception:  # noqa: BLE001
            change_rate = None

    # 52周区间位置
    pos_pct = None
    if price is not None and high52 and low52 and high52 > low52:
        pos_pct = round((price - low52) / (high52 - low52) * 100, 1)

    # 扩展估值（需财务输入）
    ps = peg = None
    fin = financials or {}
    if fin.get("revenue") and mkt_cap:
        try:
            ps = round(mkt_cap / fin["revenue"], 2)
        except Exception:  # noqa: BLE001
            ps = None
    if pe and pe > 0 and fin.get("growth_rate"):
        try:
            g = float(fin["growth_rate"])
            if g > 0:
                peg = round(pe / g, 2)
        except Exception:  # noqa: BLE001
            peg = None

    # 综合估值评分（0-100，越高越贵/越不便宜）
    score = 50
    notes = []
    if pe and pe > 0:
        if pe < 10:
            score -= 20
        elif pe < 20:
            score -= 5
        elif pe < 30:
            score += 10
        else:
            score += 25
    if pb:
        if pb < 1:
            score -= 10
        elif pb < 3:
            pass
        elif pb < 5:
            score += 8
        else:
            score += 15
    if pos_pct is not None:
        if pos_pct < 25:
            score -= 8
        elif pos_pct > 75:
            score += 8
    score = max(0, min(100, score))

    if score < 35:
        verdict = "偏低估，具备关注价值"
    elif score <= 60:
        verdict = "估值合理，中性"
    else:
        verdict = "估值偏贵，谨慎追高"

    result = {
        "code": code,
        "name": name,
        "price": round(price, 3) if price is not None else None,
        "change_rate": change_rate,
        "pe": round(pe, 2) if pe is not None else None,
        "pe_label": _pe_label(pe),
        "pb": round(pb, 2) if pb is not None else None,
        "pb_label": _pb_label(pb),
        "ps": ps,
        "peg": peg,
        "week52_high": round(high52, 3) if high52 is not None else None,
        "week52_low": round(low52, 3) if low52 is not None else None,
        "week52_position_pct": pos_pct,
        "market_cap": mkt_cap,
        "valuation_score": score,
        "verdict": verdict,
        "missing_financials": [k for k in ("revenue", "growth_rate")
                               if not fin.get(k)],
    }
    return result, None

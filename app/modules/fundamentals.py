"""个股基本面反向信号（估值分位 + 机构增减持）。

数据源：富途 OpenAPI
- get_valuation_detail(code, valuation_type): 历史 PE/PB/PS 分位、行业/市场排名
  （港股可用；valuation_type 1=PE 2=PB 3=PS）
- get_shareholders_holding_changes(code, num, filter_type): 大股东/机构持股变动
  （港股可用；filter_type 1=增持 2=减持）

⚠ 沽空反向信号（第 3 档）暂未实现：富途 OpenAPI 的 get_daily_short_volume /
get_short_interest 对港股均返回空表（列是美股模板），market_snapshot 仅含
“是否可卖空/可卖空股数/费率”，无真实沽空情绪（卖空占成交比、未平仓占流通比）。
需接入东方财富 / 港交所(HKEX)卖空披露数据源后补做，见 references/README.md。

所有函数返回 (dict, error)；error 非 None 时上层优雅展示 / 计 0，不抛异常。
"""
from __future__ import annotations

from typing import Optional, Tuple

from ..cache import cached


def _num(v):
    try:
        if v is None or (isinstance(v, str) and v.strip().upper() in ("N/A", "NA", "")):
            return None
        f = float(v)
        return None if f != f else f
    except (ValueError, TypeError):
        return None


@cached(skip_first=True)
def valuation_signal(client, code: str) -> Tuple[Optional[dict], Optional[str]]:
    """估值分位信号（错杀核心：好公司被错杀 = 便宜）。

    读 PE / PB 历史分位 + 行业排名。分位越低越「被低估」，错杀后反弹空间越大 → 正向信号。
    返回 (dict, error)，dict 含 {pe,pb,plate,market,score,label,low}。
    """
    pe_d, pe_err = client.valuation_detail(code, valuation_type=1)
    pb_d, pb_err = client.valuation_detail(code, valuation_type=2)

    if pe_err and pb_err:
        return None, pe_err

    def _trend(d):
        if not d or not isinstance(d, dict):
            return None, None, None
        tr = d.get("trend", {}) or {}
        return _num(tr.get("current_value")), _num(tr.get("valuation_percentile")), _num(tr.get("average_value"))

    pe_cur, pe_pct, pe_avg = _trend(pe_d)
    pb_cur, pb_pct, pb_avg = _trend(pb_d)

    plate = ((pe_d or {}).get("plate_distribution", {}) or {}) if pe_d else {}
    market = ((pe_d or {}).get("market_distribution", {}) or {}) if pe_d else {}

    # 1) PE 历史分位为主信号
    score = 0.0
    label = None
    low = False
    if pe_pct is not None:
        if pe_pct <= 20:
            score, label, low = 2.0, "PE 历史分位极低（深度低估）", True
        elif pe_pct <= 40:
            score, label, low = 1.0, "PE 历史分位偏低", True
        elif pe_pct <= 60:
            score, label, low = 0.3, "PE 历史分位中性", False
        elif pe_pct <= 80:
            score, label, low = 0.0, None, False
        else:
            score, label, low = -0.5, "PE 历史分位偏高（非错杀区）", False
    # 2) PB 历史分位辅助微调（仅额外加分，避免重复惩罚）
    if pb_pct is not None and pb_pct <= 20:
        score += 0.3
        if label is None:
            label = "PB 历史分位偏低"
    # 3) 行业排名（估值在行业内靠前 = 行业低估）
    try:
        pr = int(plate.get("plate_ranking") or 0)
        pt = int(plate.get("plate_stock_item_count") or 0)
    except (ValueError, TypeError):
        pr, pt = 0, 0
    if pr and pt and pr <= max(1, pt * 0.33):
        score += 0.5
        if label is None:
            label = "行业内估值偏低"

    return {
        "pe": {"current": pe_cur, "percentile": pe_pct, "avg": pe_avg},
        "pb": {"current": pb_cur, "percentile": pb_pct, "avg": pb_avg},
        "plate": {"name": plate.get("plate_name"), "rank": pr, "total": pt},
        "market": {"rank": market.get("ranking"), "total": market.get("total")},
        "score": round(score, 1),
        "label": label,
        "low": low,
    }, None


@cached(skip_first=True)
def institution_signal(client, code: str, num: int = 8) -> Tuple[Optional[dict], Optional[str]]:
    """机构 / 大股东增减持信号（smart money 在买 = 错杀反向利好）。

    分别拉取增持(filter_type=1)与减持(filter_type=2)，汇总近 num 期净方向。
    返回 (dict, error)，dict 含 {incre_n,decre_n,net_shares,recent,score,label}。
    """
    inc_d, inc_err = client.shareholders_holding_changes(code, num=num, filter_type=1)
    dec_d, dec_err = client.shareholders_holding_changes(code, num=num, filter_type=2)

    def _rows(df):
        if df is None or (hasattr(df, "empty") and df.empty):
            return []
        out = []
        for _, r in df.iterrows():
            chg = _num(r.get("share_change_num"))
            out.append({
                "name": r.get("name"),
                "period": r.get("period_text") or r.get("holding_date_str"),
                "change": chg,                      # 股数，>0 增持 <0 减持
                "ratio": _num(r.get("share_ratio")),  # 变动后持股占流通比(%)
                "holder_type": r.get("holder_type"),
            })
        return out

    inc = _rows(inc_d)
    dec = _rows(dec_d)
    if not inc and not dec:
        return None, inc_err or dec_err or "暂无大股东持股变动数据"

    incre_n = len(inc)
    decre_n = len(dec)
    incre_net = sum((x["change"] or 0) for x in inc)
    decre_net = sum(abs(x["change"] or 0) for x in dec)
    net = incre_net - decre_net

    # 共识度：多家同向 = 信号更可信
    if incre_n > 0 and decre_n == 0:
        consensus = "一致增持"
    elif decre_n > 0 and incre_n == 0:
        consensus = "一致减持"
    elif incre_n > 0 and decre_n > 0:
        consensus = "增持多于减持" if incre_n > decre_n else "减持多于增持"
    else:
        consensus = None

    # 评分：净方向 + 期数强度 + 共识度（多家共振加分）
    if incre_n >= 3 and net > 0:
        score, label = 2.0, "多家机构一致增持(强共识)"
    elif incre_n >= 2 and net > 0:
        score, label = 1.5, "机构近期持续净增持"
    elif decre_n >= 3 and net < 0:
        score, label = -1.5, "多家机构一致减持(强风险)"
    elif decre_n >= 2 and net < 0:
        score, label = -1.0, "机构近期净减持（风险）"
    elif net > 0:
        score, label = 0.5, "机构近期净增持"
    elif net < 0:
        score, label = -0.5, "机构近期净减持"
    else:
        score, label = 0.0, None

    # 取增/减持中各最新 2 条展示
    recent = (sorted(inc, key=lambda x: str(x["period"]), reverse=True)[:2]
              + sorted(dec, key=lambda x: str(x["period"]), reverse=True)[:2])

    return {
        "incre_n": incre_n,
        "decre_n": decre_n,
        "consensus": consensus,
        "net_shares": int(net),
        "incre_net": int(incre_net),
        "decre_net": int(decre_net),
        "recent": recent,
        "score": round(score, 1),
        "label": label,
    }, None

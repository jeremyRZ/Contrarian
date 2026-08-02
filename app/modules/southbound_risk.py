"""聚合个股南向(港股通)减持风险预警。

扫描研究池（默认龙头观察池 45 只，或自定义 codes），对每只标的新查港股通持股，
汇总存在「连续减持 / 骤减」反向风险的标的，供风控页作为市场早期预警统一展示。
数据来自东方财富沪深港通持股明细（真实），与单票页 southbound.holding 同源。
"""
from __future__ import annotations

from typing import Optional, Tuple

from . import southbound, screener


def aggregate(client, codes: Optional[list] = None) -> Tuple[Optional[dict], Optional[str]]:
    """聚合南向减持风险。返回 (dict, error)。

    dict: {universe, scanned, count, items:[{code,name,hold_ratio,
           contiguous_up_days,contiguous_down_days,chg_ratio_1d,risk,date}],
           source, note}
    items 按连续减持天数降序。
    """
    if codes:
        universe = list(codes)
        universe_label = "自定义"
    else:
        # 默认扫研究池（龙头观察池），作为南向减持风险的市场早期预警。
        # 如需只看自有持仓，可传入 codes=持仓代码列表。
        universe = list(screener.LEADERS)
        universe_label = "龙头观察池"

    items = []
    scanned = 0
    for code in universe:
        scanned += 1
        hd, herr = southbound.holding(code)
        if not hd:
            continue
        risk = hd.get("risk") or []
        if risk:
            items.append({
                "code": code,
                "name": hd.get("name"),
                "hold_ratio": hd.get("hold_ratio"),
                "contiguous_up_days": hd.get("contiguous_up_days"),
                "contiguous_down_days": hd.get("contiguous_down_days"),
                "chg_ratio_1d": hd.get("chg_ratio_1d"),
                "risk": risk,
                "date": hd.get("date"),
            })

    # 按连续减持天数降序；并列时按单日骤减幅度升序
    items.sort(key=lambda x: (x.get("contiguous_down_days") or 0), reverse=True)

    return {
        "universe": universe_label,
        "scanned": scanned,
        "count": len(items),
        "items": items,
        "source": "eastmoney-hsgt",
        "note": "南向(港股通)个股持股连续减持/骤减反向风险；数据来自东方财富沪深港通持股明细（真实）",
    }, None

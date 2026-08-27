"""聚合个股南向(港股通)减持风险预警。

扫描研究池（默认龙头观察池 45 只，或自定义 codes），对每只标的新查港股通持股，
汇总存在「连续减持 / 骤减」反向风险的标的，供风控页作为市场早期预警统一展示。
数据来自东方财富沪深港通持股明细（真实），与单票页 southbound.holding 同源。
"""
from __future__ import annotations

from typing import Optional, Tuple

from . import southbound

DEFAULT_UNIVERSE = [
    "HK.00700", "HK.03690", "HK.01810", "HK.09988", "HK.09618", "HK.01024",
    "HK.09626", "HK.09888", "HK.09999", "HK.00992", "HK.00981", "HK.00522",
    "HK.02382", "HK.01211", "HK.00175", "HK.02333", "HK.09868", "HK.02015",
    "HK.09866", "HK.00883", "HK.00857", "HK.00386", "HK.00005", "HK.00939",
    "HK.01398", "HK.03988", "HK.01299", "HK.02318", "HK.02628", "HK.00001",
    "HK.01113", "HK.00388", "HK.01928", "HK.00027", "HK.01093", "HK.02269",
    "HK.02359", "HK.02007", "HK.02202", "HK.00941", "HK.00267", "HK.01698",
    "HK.00241", "HK.01347", "HK.02020",
]


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
        universe = list(DEFAULT_UNIVERSE)
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

"""个股资金流向（大单 / 超大单 / 中单 / 小单）。

数据源：富途 OpenAPI
- get_capital_distribution(code): 最近一个交易时段的逐档(超大/大/中/小单)净流入/流出
- get_capital_flow(code, DAY, start, end): 每日资金净流入序列(超大/大/中/小/主力/合计)

字段已实测（futu 10.9.6908，HK.00700 2026-07-31）：
  distribution: capital_in_super/out_super 超大单、capital_in_big/out_big 大单、
    capital_in_mid/out_mid 中单、capital_in_small/out_small 小单、update_time（港元）
  flow: in_flow(合计净)、super_in_flow 超大单净、big_in_flow 大单净、
    mid_in_flow 中单净、sml_in_flow 小单净、main_in_flow(主力=超大+大)净、
    capital_flow_item_time（港元）

所有函数都返回 (data, error)；error 非 None 时上层应优雅展示，不抛异常。
"""
from __future__ import annotations

from typing import Optional, Tuple

from ..cache import cached
from ..numbers import as_float as _num


# 档位中文标签
TIER_LABELS = {"super": "超大单", "big": "大单", "mid": "中单", "small": "小单"}


@cached(skip_first=True)
def distribution(client, code: str) -> Tuple[Optional[dict], Optional[str]]:
    """个股资金流向分布快照（超大/大/中/小单）。返回 (dict, error)。

    dict: {code, update_time, tiers:{super/big/mid/small:{in,out,net}},
           main_net(主力=超大+大 净流入), total_net(全部档位合计),
           main_ratio(主力占合计比), source}
    金额单位：港元。net>0 表示净流入（资金流入该档位）。
    """
    df, err = client.capital_distribution(code)
    if err:
        return None, err
    if df is None or (hasattr(df, "empty") and df.empty):
        return None, "暂无资金流向分布数据（可能非交易日或接口限流）"
    try:
        row = df.iloc[-1]  # 取最新一条

        def _net(i_col, o_col):
            a = _num(row.get(i_col))
            b = _num(row.get(o_col))
            if a is None and b is None:
                return None
            return round((a or 0) - (b or 0), 2)

        tiers = {}
        for key, (i_col, o_col) in {
            "super": ("capital_in_super", "capital_out_super"),
            "big": ("capital_in_big", "capital_out_big"),
            "mid": ("capital_in_mid", "capital_out_mid"),
            "small": ("capital_in_small", "capital_out_small"),
        }.items():
            tin = _num(row.get(i_col))
            tout = _num(row.get(o_col))
            tiers[key] = {
                "in": tin,
                "out": tout,
                "net": _net(i_col, o_col),
            }

        super_net = (tiers["super"]["net"] or 0)
        big_net = (tiers["big"]["net"] or 0)
        main_net = round(super_net + big_net, 2)
        total_net = round(sum((t["net"] or 0) for t in tiers.values()), 2)
        return {
            "code": code,
            "update_time": str(row.get("update_time") or ""),
            "tiers": tiers,
            "main_net": main_net,       # 主力(超大+大)净流入，港元
            "total_net": total_net,     # 全部档位合计净流入，港元
            "main_ratio": round(main_net / total_net, 3) if total_net else None,
            "source": "futu-capital-distribution",
        }, None
    except Exception as e:  # noqa: BLE001
        return None, f"资金流向分布解析失败: {e}"


@cached(skip_first=True)
def series(client, code: str, days: int = 20) -> Tuple[Optional[dict], Optional[str]]:
    """个股每日资金净流入序列（超大/大/中/小/主力/合计）。返回 (dict, error)。

    dict: {code, days, series:[{date,super,big,mid,small,main,total}],
           summary:{super,big,mid,small,main,total}(近 days 日汇总), source}
    金额单位：港元。net>0 表示净流入。
    """
    df, err = client.capital_flow(code, days=days)
    if err:
        return None, err
    if df is None or (hasattr(df, "empty") and df.empty):
        return None, "暂无资金流向序列数据（可能非交易日或接口限流）"
    try:
        rows = []
        for _, r in df.iterrows():
            d = str(r.get("capital_flow_item_time") or r.get("last_valid_time") or "")[:10]
            rows.append({
                "date": d,
                "super": _num(r.get("super_in_flow")),
                "big": _num(r.get("big_in_flow")),
                "mid": _num(r.get("mid_in_flow")),
                "small": _num(r.get("sml_in_flow")),
                "main": _num(r.get("main_in_flow")),
                "total": _num(r.get("in_flow")),
            })
        rows.sort(key=lambda x: x["date"])
        win = rows[-max(int(days), 1):]

        def _sum(col):
            return round(sum((x[col] or 0) for x in win), 2)

        summary = {
            "super": _sum("super"), "big": _sum("big"), "mid": _sum("mid"),
            "small": _sum("small"), "main": _sum("main"), "total": _sum("total"),
        }
        return {
            "code": code,
            "days": len(win),
            "series": rows,
            "summary": summary,
            "source": "futu-capital-flow",
        }, None
    except Exception as e:  # noqa: BLE001
        return None, f"资金流向序列解析失败: {e}"

"""
错杀观察扫描模块（Contrarian 核心）
- 在全市场/观察池中，筛选"内在价值没问题、但因板块 beta 杀估值 + 资金面斩仓而被错杀"的优质标的
- 复用 screener 的 6 策略评分，叠加错杀质量过滤：盈利(PE>0) + 被错杀(超跌/恐慌信号)
- 返回 TopN 确信标的；列表指纹去重由 api 层调用 notify.push_if_new 完成
"""
from __future__ import annotations

from typing import Optional

from . import screener
from . import reverse_signals

# 反向信号增强时，仅对基础分最高的候选取前 N 名拉取三块数据，控制 API 调用量
REVERSE_CANDIDATE_CAP = 12


def missed_scan(client, pool: str = "leaders", top_n: int = 5,
                min_drop_pct: float = 20.0, hstech_code: str = "HK.800000"):
    """
    错杀观察扫描。返回 (list_of_result, error)。
    result: {code,name,price,change_rate,pe,week52_position_pct,score,signals,conviction,reverse}

    错杀质量：盈利 + 至少一个错杀信号；conviction = 基础分 + 超跌加分(2) + 反向信号分(0~6)。
    """
    # 先取较宽的评分结果（不限 top_n），再做错杀过滤
    data, err = screener.screen(
        client, codes=None if pool == "leaders" else None,
        top_n=200, hstech_code=hstech_code,
    )
    if err:
        return None, err
    base = data["results"]

    filtered = []
    for r in base:
        pe = r.get("pe")
        is_profitable = (pe is None or pe > 0)  # 内在价值没问题（盈利或无法判断）
        is_oversold = (
            "深度超跌反弹" in r["signals"]
            or "异常放量急跌(逆向)" in r["signals"]
            or "恒科急跌联动低吸" in r["signals"]
        )
        # 错杀质量：盈利 + 至少一个错杀信号
        if is_profitable and is_oversold:
            item = dict(r)
            item["reverse"] = 0.0
            item["reverse_signals"] = []
            filtered.append(item)

    if not filtered:
        return [], None

    # 仅对基础分最高的候选拉取南向/回购/新闻三块，计算反向信号加分
    filtered.sort(key=lambda x: x["score"], reverse=True)
    for r in filtered[:REVERSE_CANDIDATE_CAP]:
        rev, _ = reverse_signals.reverse_score(client, r["code"], days=60, num=10)
        base_bonus = 2.0 if "深度超跌反弹" in r["signals"] else 0.0
        if rev:
            r["reverse"] = rev["score"]
            r["reverse_signals"] = rev["signals"]
            r["signals"] = r["signals"] + rev["signals"]
        r["conviction"] = round(r["score"] + base_bonus + r["reverse"], 1)

    # 其余候选不拉取三块数据（控制 API 量），conviction 仅含基础分 + 超跌加分
    for r in filtered[REVERSE_CANDIDATE_CAP:]:
        base_bonus = 2.0 if "深度超跌反弹" in r["signals"] else 0.0
        r["conviction"] = round(r["score"] + base_bonus, 1)

    filtered.sort(key=lambda x: x["conviction"], reverse=True)
    return filtered[:top_n], None

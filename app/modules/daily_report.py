"""每日持仓资金面背离报告。

遍历持仓正股（复用 monitor 的衍生品过滤，跳过窝轮/杠杆 ETF），逐票跑
divergence.analyze_divergence，汇总成 markdown 报告并推送企业微信。
"""
from __future__ import annotations

import logging
from typing import List, Tuple

from . import monitor, divergence, filters
from .. import notify

logger = logging.getLogger("hk-daily-report")


def _holdings_stocks(client) -> Tuple[List[Tuple[str, str]], Optional[str]]:
    """取持仓正股列表 [(code, name)]，过滤窝轮/杠杆 ETF 与停牌/无报价标的。返回 (list, error)。"""
    pos, err = client.positions()
    if err:
        return [], err
    if pos is None or (hasattr(pos, "empty") and pos.empty):
        return [], None
    cols = {c.lower(): c for c in pos.columns}
    c_code = cols.get("code")
    c_name = cols.get("stock_name") or cols.get("name")
    stocks: List[Tuple[str, str]] = []
    skipped = 0
    for _, row in pos.iterrows():
        code = str(row[c_code])
        name = str(row[c_name]) if c_name else code
        ptype = monitor._classify(name, code)
        if ptype in ("窝轮", "杠杆ETF"):
            skipped += 1
            continue
        # 自动剔除停牌 / 无报价 / 无估值标的（与 /holdings、/screener 一致）
        tradable, reason = filters.is_tradable(client, code)
        if not tradable:
            skipped += 1
            continue
        stocks.append((code, name))
    if skipped:
        logger.info("[DailyReport] 跳过 %d 只衍生品/停牌/无报价标的", skipped)
    return stocks, None


def build_daily_report(client) -> dict:
    """生成每日持仓资金面背离报告（不推送）。

    全持仓展开明细（按严重度分组：背离 → 偏弱 → 平稳），并受微信单条消息
    4096 字节限制保护：平稳票明细超出预算时折叠为名单列表，保证消息可送达。

    返回 {ok, total, divergence_count, weak_count, calm_count, stocks:[...], markdown}
    """
    stocks, err = _holdings_stocks(client)
    if err:
        return {"ok": False, "error": err, "total": 0, "divergence_count": 0,
                "weak_count": 0, "calm_count": 0, "stocks": [], "markdown": ""}
    stocks = stocks or []
    results = []
    for code, name in stocks:
        try:
            r = divergence.analyze_divergence(client, code, name=name)
        except Exception as e:  # noqa: BLE001
            r = {"code": code, "name": name, "divergence": False, "bearish": False,
                 "markdown": f"### {name} ({code})\n\n分析异常：{e}"}
        results.append(r)

    divs = [r for r in results if r.get("divergence")]
    weak = [r for r in results if r.get("bearish") and not r.get("divergence")]
    calm = [r for r in results if not r.get("bearish")]

    # 微信 markdown 单条上限 ~4096 字节，留余量到 3800
    MAX_BYTES = 3800

    def _names(lst):
        return "、".join(f"{r['name']}({r['code']})" for r in lst)

    lines = [f"**📡 每日持仓资金面背离扫描（{len(stocks)} 只正股）**", ""]

    def _fits(block: str) -> bool:
        return len("\n".join(lines).encode("utf-8")) + len(block.encode("utf-8")) <= MAX_BYTES

    if divs:
        lines.append(f"🔴 **背离预警 {len(divs)} 只**：{_names(divs)}")
        lines.append("")
        for r in divs:
            lines.append(r["markdown"]); lines.append("---")
    if weak:
        lines.append(f"🟡 **资金面偏弱 {len(weak)} 只**：{_names(weak)}")
        lines.append("")
        for r in weak:
            lines.append(r["markdown"]); lines.append("---")
    if calm:
        lines.append(f"🟢 **资金面平稳 {len(calm)} 只**：{_names(calm)}")
        lines.append("")
        shown = 0
        for r in calm:
            block = r["markdown"] + "\n---"
            if _fits(block):
                lines.append(block); shown += 1
            else:
                break
        if shown < len(calm):
            lines.append(f"（其余 {len(calm) - shown} 只平稳票省略明细）")
    if not stocks:
        lines.append("（无持仓正股，跳过）")

    markdown = "\n".join(lines).rstrip("\n")
    return {
        "ok": True,
        "total": len(stocks),
        "divergence_count": len(divs),
        "weak_count": len(weak),
        "calm_count": len(calm),
        "stocks": results,
        "markdown": markdown,
    }


def run_daily_report(client, webhook: str = "", *, funds_note: str = "") -> dict:
    """生成报告并推送企业微信（带指纹去重，同一份报告约 20h 内只推一次）。

    返回报告 dict + pushed 标志（无 webhook 时降级为服务端日志，pushed=False）。
    """
    rep = build_daily_report(client)
    if not rep.get("ok"):
        return rep
    fp = "daily-div:" + "|".join(r["code"] for r in rep["stocks"][:8])
    pushed = False
    if webhook:
        message = rep["markdown"] + (f"\n{funds_note}" if funds_note else "")
        pushed = notify.push_if_new(fp, message, webhook, min_interval=3600 * 20)
    else:
        logger.warning("[DailyReport-降级] %s", rep["markdown"].replace("\n", " | "))
    rep["pushed"] = pushed
    return rep

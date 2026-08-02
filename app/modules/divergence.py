"""持仓资金面「中短期背离」分析。

检测「南向连续减持」与「盘中资金流单日净流入但超大单流出 + 近 15 日主力累计流出」
的冲突形态（参考交易员多因子权重框架）：

    利空权重(≈70%)：南向连续减持 + 近 15 日主力累计净流出 + 当日超大单净流出
    利好权重(≈30%)：单日主力净流入（主要由大单拉动，超大单在跑 = 伪利好/诱多）

输出结构化判定 + 可直接推送企业微信的 markdown 文本。
所有外部数据调用失败都优雅降级（该项计缺失），不抛异常。
"""
from __future__ import annotations

from typing import Optional

from . import southbound, capital_flow

# ---------- 阈值（可调，集中于此便于维护）----------
SOUTH_DOWN_MIN_DAYS = 2        # 南向连续减持趋势起点
SOUTH_DOWN_STRONG_DAYS = 3     # 强信号阈值
SOUTH_SINGLE_CHG_RATIO = -1.0  # 单日南向骤减阈值 (%)
FLOW_DAYS = 15                 # 主力累计窗口（交易日）


def _fmt_yi(v: Optional[float]) -> str:
    """港元 -> 亿元字符串（保留 2 位，带正负号）。None 返回 '—'。"""
    if v is None:
        return "—"
    return f"{v / 1e8:+.2f}亿"


def _fmt_wan(v: Optional[float]) -> str:
    """港元 -> 万元字符串（保留 1 位，带正负号）。None 返回 '—'。"""
    if v is None:
        return "—"
    return f"{v / 1e4:+.1f}万"


def analyze_divergence(client, code: str, name: str = "") -> dict:
    """对单只票做资金面背离分析。

    返回结构化 dict（含可直接推送企业微信的 `markdown` 字段）：
      - divergence: 是否典型「中短期背离」（中线偏空 + 单日伪利好）
      - bearish / pseudo_bull: 利空 / 伪利好标志
      - south_down_days / flow15_main / super_net / big_net / day_main_net ...
      - bearish_reasons / bullish_reasons: 文字理由
      - verdict: 综合判定文字
      - markdown: 企业微信 markdown 全文
    """
    # ---- 拉数据（各自优雅降级）----
    sb, sb_err = southbound.holding(code)
    dist, dist_err = capital_flow.distribution(client, code)
    flow, flow_err = capital_flow.series(client, code, days=FLOW_DAYS)

    sb = sb or {}
    dist = dist or {}
    flow = flow or {}
    name = name or sb.get("name") or code

    # ---- 南向 ----
    sb_down_days = int(sb.get("contiguous_down_days", 0) or 0)
    sb_up_days = int(sb.get("contiguous_up_days", 0) or 0)
    sb_chg_ratio = sb.get("chg_ratio_1d")
    sb_chg_shares = sb.get("chg_shares_1d")

    south_trend = sb_down_days >= SOUTH_DOWN_MIN_DAYS            # 南向连续减持趋势
    south_strong = sb_down_days >= SOUTH_DOWN_STRONG_DAYS
    south_single_drop = (sb_chg_ratio is not None and sb_chg_ratio <= SOUTH_SINGLE_CHG_RATIO)

    # ---- 资金流（逐档）----
    tiers = dist.get("tiers") or {}
    main_net = dist.get("main_net")
    total_net = dist.get("total_net")
    super_net = (tiers.get("super") or {}).get("net")
    big_net = (tiers.get("big") or {}).get("net")

    day_main_up = main_net is not None and main_net > 0
    super_out = super_net is not None and super_net < 0
    big_in = big_net is not None and big_net > 0
    # 单日主力净流入但超大单流出、大单拉起 = 伪利好（游资护盘/散户跟风）
    pseudo_bull = day_main_up and super_out and big_in

    # ---- 15 日累计 ----
    summary = flow.get("summary") or {}
    sum_main = summary.get("main")
    sum_super = summary.get("super")
    trend_15_out = (sum_main is not None and sum_main < 0)        # 15 日主力累计净流出
    trend_15_super_out = (sum_super is not None and sum_super < 0)

    # ---- 冲突判定（中短期背离）----
    bearish = south_trend or trend_15_out or super_out
    divergence = bearish and pseudo_bull   # 中线偏空 + 单日伪利好 = 典型背离

    # ---- 文字生成 ----
    # 南向段
    if sb_err:
        if "非港股通" in (sb_err or ""):
            sb_tag, sb_line = "不适用", "非港股通标的，无南向数据"
        else:
            sb_tag, sb_line = "未知", f"南向数据获取失败：{sb_err}"
    else:
        parts = []
        if sb_down_days > 0:
            parts.append(f"已连续 {sb_down_days} 日减持")
        if sb_chg_shares is not None and sb_chg_ratio is not None:
            parts.append(f"当日 {sb_chg_shares / 1e4:+.1f}万股 ({sb_chg_ratio:+.2f}%)")
        if sb_up_days >= 2:
            parts.append(f"连续 {sb_up_days} 日增持")
        if not parts:
            parts.append("当日持股平稳")
        if south_trend or south_single_drop:
            sb_tag = "偏空"
        elif sb_up_days >= 2:
            sb_tag = "偏多"
        else:
            sb_tag = "中性"
        sb_line = "；".join(parts)

    # 资金流段
    if dist_err:
        flow_tag, flow_line = "未知", f"资金流数据获取失败：{dist_err}"
    else:
        flow_line = (f"当日主力净流 {_fmt_wan(main_net)}，合计 {_fmt_wan(total_net)}；"
                     f"超大单 {_fmt_wan(super_net)}，大单 {_fmt_wan(big_net)}")
        flow_tag = "伪利好(隐患)" if pseudo_bull else ("偏多" if day_main_up else ("偏空" if (main_net or 0) < 0 else "中性"))

    # 15 日段
    if flow_err:
        t15_tag, t15_line = "未知", f"15 日序列获取失败：{flow_err}"
    else:
        t15_line = f"近 {FLOW_DAYS} 日主力累计 {_fmt_yi(sum_main)}（超大单 {_fmt_yi(sum_super)}）"
        t15_tag = "大幅流出" if trend_15_out else ("净流入" if (sum_main or 0) > 0 else "中性")

    # 综合判定
    bearish_reasons = []
    if south_trend:
        bearish_reasons.append(f"南向连续减持 {sb_down_days} 日" + ("（强）" if south_strong else ""))
    if south_single_drop:
        bearish_reasons.append(f"单日南向骤减 {abs(sb_chg_ratio):.1f}%")
    if trend_15_out:
        bearish_reasons.append(f"近 {FLOW_DAYS} 日主力累计净流出 {_fmt_yi(sum_main)}")
    if super_out:
        bearish_reasons.append("当日超大单净流出")

    bullish_reasons = []
    if pseudo_bull:
        bullish_reasons.append("单日主力净流入由大单拉动、超大单在跑（游资护盘/散户跟风，伪利好）")

    if divergence:
        verdict = ("典型「中短期背离」：中线资金撤退未止，单日反弹属诱多。"
                   "不符合「边际买盘进场」超跌低吸条件，不要入场；"
                   "等待南向止减转增持 + 超大单净流出转正，才是底线进场信号。")
    elif bearish and not pseudo_bull:
        verdict = "资金面中线偏空（无单日伪利好掩护），暂不符合低吸条件，观望。"
    elif pseudo_bull and not bearish:
        verdict = "单日资金面偏多但缺乏中线支撑信号，谨慎，不构成强低吸信号。"
    else:
        verdict = "资金面未见明显背离，维持现有观察。"

    # markdown（企业微信）
    md = [f"### {name} ({code})"
          + ("  🔴 资金面背离" if divergence else ("  🟡 资金面偏弱" if bearish else "  🟢 资金面平稳"))]
    md.append("")
    md.append(f"**南向/港股通（{sb_tag}）**：{sb_line}")
    md.append(f"**盘中资金流向（{flow_tag}）**：{flow_line}")
    md.append(f"**近 {FLOW_DAYS} 日主力趋势（{t15_tag}）**：{t15_line}")
    md.append("")
    md.append(f"**综合判定**：{verdict}")
    if bearish_reasons:
        md.append(f"- 利空权重≈70%：{'；'.join(bearish_reasons)}")
    if bullish_reasons:
        md.append(f"- 利好权重≈30%（伪利好）：{'；'.join(bullish_reasons)}")
    markdown = "\n".join(md)

    return {
        "code": code,
        "name": name,
        "divergence": divergence,
        "bearish": bearish,
        "pseudo_bull": pseudo_bull,
        "south_down_days": sb_down_days,
        "south_single_drop": south_single_drop,
        "day_main_net": main_net,
        "super_net": super_net,
        "big_net": big_net,
        "flow15_main": sum_main,
        "flow15_super": sum_super,
        "bearish_reasons": bearish_reasons,
        "bullish_reasons": bullish_reasons,
        "verdict": verdict,
        "markdown": markdown,
        "errors": [e for e in [sb_err, dist_err, flow_err] if e],
    }

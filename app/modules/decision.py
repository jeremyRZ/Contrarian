"""LLM 决策层（M3）—— 回测门控 + 差异化判断。

衔接契约（前一阶段输出 → 本阶段输入）：
- 输入：/analyze 的实时特征（price/chg/pe/pb/52w/technical/conclusion/signals）
        + reverse_signals 的错杀反向信号构成
        + 最近一次宽股票池技术回测的可信度（整体胜率/样本/徽章）
- 门控：仅当「回测整体胜率 ≥ config.llm.min_win_rate 且 可信度 ≠ 样本不足」
        才调用 LLM，避免对未验证信号浪费 token，也防止 LLM 在无效信号上瞎编。
- 输出：结构化 decision（verdict / per_signal / rationale / risk / position），
        供 analyze.html「🤖 猎手观点」卡片展示。

与 news.py 的 LLM 复核区别：本模块是「决策」而非「情绪标注」，
输入是聚合后的多源信号 + 回测验证结论，输出是带仓位建议的可执行判断。
"""
from __future__ import annotations

import json
from typing import Optional

from . import analyze, reverse_signals, backtest, strategy_config, llm_client
from ..futu_client import load_config as _load_config


def _gating_ok(backtest_summary: dict, min_win_rate: float) -> Tuple[bool, str]:
    """回测门控：是否值得调用 LLM。返回 (通过?, 原因)。

    min_win_rate 来自 strategies.yaml.llm.min_win_rate（UI 可调，百分比）。
    """
    min_win = float(min_win_rate)
    if not backtest_summary:
        return False, "无回测数据，跳过 LLM（信号未经验证）"
    overall = backtest_summary.get("overall") or {}
    conf = overall.get("confidence") or ""
    n = overall.get("n")
    win = overall.get("win_rate")
    if n is None or n == 0:
        return False, "回测样本为 0（该标的近 250 日未触发买入信号），跳过 LLM"
    if "样本不足" in conf:
        return False, f"回测样本不足(n={n})无统计意义，跳过 LLM"
    if win is None or win < min_win:
        return False, f"回测整体胜率 {win}% < 门槛 {min_win}%，信号未达可信阈值，跳过 LLM"
    return True, f"回测可信（胜率 {win}% / {conf}），已调用 LLM"


def _backtest_brief(backtest_summary: dict, cfg: dict) -> str:
    """把回测结论压缩成给 LLM 的简短上下文。"""
    if not backtest_summary:
        return "（无回测数据）"
    o = backtest_summary.get("overall") or {}
    lines = [
        f"回测整体：样本 {o.get('n')} 笔，胜率 {o.get('win_rate')}%，"
        f"平均前向收益 {o.get('avg_ret')}%，盈亏比 {o.get('profit_factor')}，"
        f"置信度 {o.get('confidence')}（持有 {backtest_summary.get('forward_days')} 日）",
    ]
    ps = backtest_summary.get("per_strategy") or {}
    fired = []
    for name, s in ps.items():
        if s.get("n"):
            fired.append(f"{name}: 样本{s['n']}/胜率{s.get('win_rate')}%/均收益{s.get('avg_ret')}%")
    if fired:
        lines.append("各策略表现：" + "；".join(fired))
    return "\n".join(lines)


def _build_messages(code: str, a: dict, rev: Optional[dict], bt: dict, cfg: dict):
    name = a.get("name") or code
    t = a.get("technical") or {}
    signals = a.get("signals") or []
    rev_score = (rev or {}).get("score")
    rev_signals = (rev or {}).get("signals") or []
    pos = a.get("week52_position_pct")

    facts = [
        f"标的：{name}（{code}）",
        f"现价：{a.get('price')}（日内 {a.get('change_rate')}%）",
        f"估值：PE {a.get('pe')} / PB {a.get('pb')}",
        f"52周区间：{a.get('week52_low')} ~ {a.get('week52_high')}（当前处于 {pos}% 分位）",
        f"技术面：MA5/10/20={t.get('ma5')}/{t.get('ma10')}/{t.get('ma20')}，"
        f"RSI14={t.get('rsi14')}，量比={t.get('vol_ratio')}",
        f"技术信号：{('、'.join(signals)) if signals else '无'}",
        f"错杀反向信号总分：{rev_score}（各档：{('、'.join(rev_signals)) if rev_signals else '无'}）",
        f"系统结论：{a.get('conclusion')}",
    ]
    system = (
        "你是港股逆向投资（Contrarian）决策助手，服务于「错杀猎手」系统。"
        "用户提供了某只港股的实时技术面、错杀反向信号（利好加分/利空扣分的多源验证）"
        "以及该信号的历史回测验证结论。请基于这些客观数据做差异化判断，不要编造数据。"
        "区分「信号已触发」与「信号未触发」：只对真实出现的信号给建议。"
        "必须返回 JSON："
        "{\"verdict\":\"低吸|谨慎低吸|观望|回避\","
        "\"confidence\":\"高|中|低\","
        "\"per_signal\":[{\"signal\":\"信号名\",\"view\":\"对它的独立判断\",\"action\":\"建议\"}],"
        "\"rationale\":\"综合逻辑（2-4句）\","
        "\"risk\":\"主要风险（1-2句）\","
        "\"position_suggestion\":\"仓位/节奏建议（1句）\"}"
    )
    user = (
        "【客观数据】\n" + "\n".join(facts) + "\n\n"
        "【回测验证】\n" + _backtest_brief(bt, cfg) + "\n\n"
        "请输出上述 JSON 决策。verdict 决定整体态度；per_signal 只对真实出现的信号给差异化判断；"
        "若回测胜率偏低或样本不足，请在 risk 中明确指出「历史有效性存疑」。"
    )
    return system, user


def _parse_llm(content: str) -> dict:
    """把 LLM 文本解析为结构化 dict。解析失败则回退到原始文本。"""
    if not content:
        return {"verdict": None, "decision": None, "parse_ok": False}
    # 尝试提取 ```json ... ``` 或裸 JSON
    raw = content.strip()
    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            obj = json.loads(raw[start:end + 1])
            obj["parse_ok"] = True
            obj["decision"] = content  # 保留原文
            return obj
        except Exception:  # noqa: BLE001
            pass
    return {"verdict": None, "decision": content, "parse_ok": False}


def decide(code: str, client=None, cfg: Optional[dict] = None,
           analysis_result: Optional[dict] = None,
           reverse_result: Optional[dict] = None) -> dict:
    """端到端决策：分析 → 反向信号 → 回测门控 → （可选）LLM。

    analysis_result / reverse_result 可由聚合接口传入，避免重复调用行情和外部数据源。

    配置来源：
    - 回测 / 信号参数来自 strategy_config（strategies.yaml + 默认值）；
      调用 backtest.cached_report 时传 cfg=None 让其内部加载，避免重复传错格式。
    - llm 门控阈值与密钥来自 config.yaml（raw），由调用方传入或此处加载。

    返回：
    {
      "code", "gated": bool, "reason": str,
      "model": str|null, "verdict": str|null,
      "per_signal": [...], "rationale", "risk", "position_suggestion",
      "decision": str|null, "parse_ok": bool,
      "backtest": {win_rate, avg_ret, confidence, n}|null
    }
    """
    if client is None:
        from .. import futu_client
        client = futu_client.build_client_from_config()
    # raw = config.yaml（含 llm 密钥/端点）；scfg = strategies.yaml（含 llm 门控阈值/开关、策略参数）
    raw = cfg or _load_config()
    scfg = strategy_config.load_config()
    llm_raw = (raw.get("llm") or {})
    llm_cfg = (scfg.get("llm") or {})
    llm_enabled = bool(llm_raw.get("enabled", True)) and bool(llm_cfg.get("enabled", True)) \
        and bool(llm_raw.get("api_key"))
    min_win = float(llm_cfg.get("min_win_rate", llm_raw.get("min_win_rate", 45.0)))

    # 1) 实时分析
    if analysis_result is None:
        a, aerr = analyze.analyze(client, code)
    else:
        a, aerr = analysis_result, None
    if not a:
        return {"code": code, "gated": True, "reason": "分析失败：" + str(aerr),
                "model": None, "verdict": None, "per_signal": [],
                "rationale": None, "risk": None, "position_suggestion": None,
                "decision": None, "parse_ok": False, "backtest": None}

    # 2) 错杀反向信号
    if reverse_result is None:
        rev, _ = reverse_signals.reverse_score(client, code, days=60, num=10)
    else:
        rev = reverse_result

    # 3) 仅采用宽股票池的技术回测作为证据。单票样本通常过少，也会污染全局证据缓存。
    bt = backtest.get_cached_evidence_report() or {}
    bt_brief = {
        "win_rate": (bt.get("overall") or {}).get("win_rate"),
        "avg_ret": (bt.get("overall") or {}).get("avg_ret"),
        "confidence": (bt.get("overall") or {}).get("confidence"),
        "n": (bt.get("overall") or {}).get("n"),
    }

    # 4) 门控（用 strategies.yaml 的 llm.min_win_rate）
    ok, reason = _gating_ok(bt, min_win)
    if not ok:
        return {"code": code, "gated": True, "reason": reason,
                "model": None, "verdict": None, "per_signal": [],
                "rationale": None, "risk": None, "position_suggestion": None,
                "decision": None, "parse_ok": False, "backtest": bt_brief}

    # 5) 调 LLM（未配置则降级）
    if not llm_enabled:
        return {"code": code, "gated": True,
                "reason": "回测门控通过，但未启用/未配置 LLM（config.llm.api_key 或 strategies.yaml.llm.enabled），跳过调用",
                "model": None, "verdict": None, "per_signal": [],
                "rationale": None, "risk": None, "position_suggestion": None,
                "decision": None, "parse_ok": False, "backtest": bt_brief}

    system, user = _build_messages(code, a, rev, bt, raw)
    content, err = llm_client.chat(system, user, raw)
    if content is None:
        return {"code": code, "gated": True,
                "reason": "回测门控通过，但 LLM 调用失败：" + str(err),
                "model": llm_raw.get("model"),
                "verdict": None, "per_signal": [],
                "rationale": None, "risk": None, "position_suggestion": None,
                "decision": None, "parse_ok": False, "backtest": bt_brief}

    parsed = _parse_llm(content)
    parsed.update({
        "code": code, "gated": False, "reason": reason,
        "model": llm_raw.get("model"),
        "backtest": bt_brief,
    })
    return parsed

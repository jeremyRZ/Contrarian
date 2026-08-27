"""Governance for read-only strategy signals and trade notifications.

Only strategies in ``PRODUCTION_STRATEGIES`` may create an actionable trade
intent or a trade notification.  Research models can expose observations, but
they cannot publish BUY/SELL language or override the production decision.
"""
from __future__ import annotations

PRODUCTION_STRATEGIES = {
    "xiaomi_trend_v1": "小米专属趋势",
    "hk_liquid_trend_rotation_v2": "港股200日风险调整动量",
    "hk_long_term_high_breakout_v1": "港股长期新高突破",
}

RESEARCH_MODELS = {
    "xiaomi_momentum_20d_v1": "小米20日方向观察",
    "xiaomi_option_selector_v1": "小米期权研究选择器",
}

ACTION_TO_INTENT = {
    "BUY": "OPEN_LONG",
    "SELL": "EXIT_LONG",
    "REVIEW": "REBALANCE",
    "HOLD": "HOLD_LONG",
    "WAIT": "NO_TRADE",
    "CASH": "NO_TRADE",
    "BLOCKED": "NO_TRADE",
    "UNAVAILABLE": "NO_TRADE",
}


def annotate_production_strategy(strategy: dict) -> dict:
    """Attach explicit decision authority and unambiguous trade semantics."""
    strategy_id = str(strategy.get("id") or "")
    is_production = strategy_id in PRODUCTION_STRATEGIES
    action = str(strategy.get("action") or "UNAVAILABLE").upper()
    strategy["decision_role"] = "PRODUCTION" if is_production else "RESEARCH_ONLY"
    strategy["trade_intent"] = ACTION_TO_INTENT.get(action, "NO_TRADE")
    strategy["actionable"] = bool(is_production and action in {"BUY", "SELL", "REVIEW"})
    strategy["decision_authority"] = "策略中心唯一正式信号源" if is_production else "无交易决策权"
    return strategy


def research_observation(model_id: str, *, as_of: str, state: int,
                         value_pct: float | None = None) -> dict:
    """Represent research direction without overloaded BUY/SELL terminology."""
    label = "BULLISH" if state > 0 else "BEARISH" if state < 0 else "NEUTRAL"
    return {
        "id": model_id,
        "name": RESEARCH_MODELS.get(model_id, model_id),
        "as_of": as_of,
        "decision_role": "RESEARCH_ONLY",
        "observation": label,
        "observed_state": int(state),
        "observed_value_pct": value_pct,
        "action": "NO_TRADE",
        "trade_intent": "NO_TRADE",
        "actionable": False,
        "decision_authority": "无交易决策权",
    }


def production_notifications(status: dict) -> list[tuple[str, str]]:
    """Build notifications only from the canonical strategy-centre payload."""
    rows: list[tuple[str, str]] = []
    if (status.get("data_freshness") or {}).get("status") != "CURRENT":
        return rows
    if status.get("mode") != "READ_ONLY_PAPER_ADVICE":
        return rows
    for strategy in status.get("strategies") or []:
        strategy_id = str(strategy.get("id") or "")
        action = str(strategy.get("action") or "").upper()
        if strategy_id not in PRODUCTION_STRATEGIES or action not in {"BUY", "SELL", "REVIEW"}:
            continue
        as_of = str(strategy.get("as_of") or "unknown")
        name = str(strategy.get("name") or PRODUCTION_STRATEGIES[strategy_id])
        intent = ACTION_TO_INTENT[action]
        lines = [
            f"**正式策略信号：{name}（{action}）**",
            f"信号日：{as_of}",
            f"交易意图：{intent}",
            f"原因：{strategy.get('reason') or '未提供'}",
        ]
        if strategy_id == "xiaomi_trend_v1":
            qty = int(strategy.get("suggested_qty") or strategy.get("held_qty") or 0)
            price = strategy.get("price")
            lines.append(f"标的：HK.01810 小米集团-W；建议股数：{qty}股")
            if price is not None:
                lines.append(f"信号价：{float(price):.2f}港币；预计金额：{qty * float(price):.2f}港币")
        elif strategy_id == "hk_liquid_trend_rotation_v2":
            orders = strategy.get("orders") or []
            lines.append(f"调仓差异：{len(orders)}项；逐项登录今日决策页面复核")
        else:
            candidates = strategy.get("candidates") or []
            lines.append(f"候选数量：{len(candidates)}；逐项登录今日决策页面复核")
        lines.append("只读模拟建议；真实交易必须由用户确认。")
        rows.append((f"production:{strategy_id}:{as_of}:{action}", "\n".join(lines)))
    return rows


def governance_summary(*, notification_configured: bool) -> dict:
    return {
        "source_of_truth": "strategy-center/status",
        "production_strategy_ids": list(PRODUCTION_STRATEGIES),
        "research_model_ids": list(RESEARCH_MODELS),
        "sell_semantics": "EXIT_LONG_ONLY",
        "short_entries_enabled": False,
        "research_can_override": False,
        "research_can_notify_trade": False,
        "real_order_submission": False,
        "notification": {
            "channel": "WECOM",
            "configured": bool(notification_configured),
            "wecom_configured": bool(notification_configured),
            "local_fallback_enabled": False,
            "status": "READY_WECOM" if notification_configured else "NOT_CONFIGURED",
        },
    }

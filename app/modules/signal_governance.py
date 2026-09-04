"""Governance for read-only strategy signals and trade notifications.

Only manually approved ``PRODUCTION`` strategies may create an actionable
trade intent or notification. Paper validation never promotes itself.
"""
from __future__ import annotations

RESEARCH_MODELS = {
    "xiaomi_momentum_20d_v1": "小米20日方向观察",
    "xiaomi_option_selector_v1": "小米期权研究选择器",
    "hk_liquid_trend_rotation_v2": "港股200日风险调整动量观察",
    "hk_long_term_high_breakout_v1": "港股长期新高突破观察",
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
    lifecycle = str(strategy.get("lifecycle") or "RESEARCH").upper()
    is_production = lifecycle == "PRODUCTION"
    action = str(strategy.get("action") or "UNAVAILABLE").upper()
    strategy["lifecycle"] = lifecycle
    strategy["decision_role"] = "PRODUCTION" if is_production else lifecycle
    strategy["observed_trade_intent"] = ACTION_TO_INTENT.get(action, "NO_TRADE")
    strategy["trade_intent"] = (strategy["observed_trade_intent"]
                                if is_production else "NO_TRADE")
    strategy["actionable"] = bool(is_production and action in {"BUY", "SELL", "REVIEW"})
    strategy["decision_authority"] = (
        "策略中心正式信号源" if is_production else
        "仅影子验证，无真实交易决策权" if lifecycle == "PAPER_VALIDATING" else
        "无交易决策权")
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
        if not strategy.get("actionable") or strategy.get("lifecycle") != "PRODUCTION":
            continue
        as_of = str(strategy.get("as_of") or "unknown")
        name = str(strategy.get("name") or strategy_id)
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


def watchlist_notifications(status: dict) -> list[tuple[str, str]]:
    """Daily discovery digest; explicitly non-actionable when execution gates fail."""
    if (status.get("data_freshness") or {}).get("status") != "CURRENT":
        return []
    rotation = next((item for item in status.get("strategies") or []
                     if item.get("id") == "hk_liquid_trend_rotation_v2"), None)
    breakout = next((item for item in status.get("strategies") or []
                     if item.get("id") == "hk_long_term_high_breakout_v1"), None)
    candidates = (rotation or {}).get("candidates") or []
    breakouts = (breakout or {}).get("candidates") or []
    if not rotation or (not candidates and not breakouts):
        return []
    top = candidates[:4]
    market = rotation.get("market") or {}
    gap = market.get("distance_to_ma_pct")
    lines = [
        f"**港股每日新股扫描｜{rotation.get('as_of')}**",
        (f"恒指MA200门控通过；距离下次轮动复核{rotation.get('next_review_date') or '待定'}。"
         if market.get("eligible") else
         f"恒指低于MA200{abs(float(gap or 0)):.2f}%；以下仅作观察，不执行买入。"),
    ]
    for index, item in enumerate(top, 1):
        qty = int(item.get("reference_qty") or 0)
        amount = qty * float(item.get("price") or 0)
        reject = (item.get("rejection_reasons") or
                  (["组合可用现金无法同时容纳整手"] if not qty else []))
        lines.append(
            f"{index}. {item.get('name')}({item.get('code')})｜"
            f"200日动量{float(item.get('momentum_pct') or 0):+.1f}%｜"
            f"风险调整分{float(item.get('score') or 0):.2f}｜"
            + (f"资金参考{qty}股/HK${amount:,.0f}" if qty else
               f"暂不可整手配置：{'；'.join(reject)}"))
    for item in breakouts[:2]:
        qty = int(item.get("reference_qty") or 0)
        lines.append(
            f"突破候选：{item.get('name')}({item.get('code')})｜"
            f"放量{float(item.get('volume_ratio') or 0):.2f}倍｜"
            + (f"资金参考{qty}股" if qty else
               f"未给股数：{(item.get('sizing') or {}).get('reason') or '风险预算不允许一手'}"))
    lines.append("这是每日发现报告，不是交易指令；正式动作仍需策略门槛和用户确认。")
    codes = "|".join(str(item.get("code")) for item in [*top, *breakouts[:2]])
    return [(f"watchlist:hk_rotation:{rotation.get('as_of')}:{codes}", "\n".join(lines))]


def governance_summary(*, notification_configured: bool,
                       strategies: list[dict] | None = None) -> dict:
    lifecycles = {str(item.get("id")): str(item.get("lifecycle") or "RESEARCH")
                  for item in (strategies or []) if item.get("id")}
    production_ids = [key for key, value in lifecycles.items() if value == "PRODUCTION"]
    return {
        "source_of_truth": "strategy-center/status",
        "production_strategy_ids": production_ids,
        "strategy_lifecycles": lifecycles,
        "promotion_policy": "MANUAL_APPROVAL_AFTER_GATE",
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

"""Time-aware reminders for already-qualified formal strategy signals."""
from __future__ import annotations

from datetime import datetime, time


def execution_phase(now: datetime) -> str:
    current = now.time()
    if current < time(9, 30):
        return "PREOPEN"
    if current <= time(10, 0):
        return "OPEN_WINDOW"
    return "LATE_DO_NOT_CHASE"


def build(status: dict, *, now: datetime | None = None,
          option_review: dict | None = None, live_price: float | None = None) -> dict | None:
    now = now or datetime.now()
    xiaomi = next((item for item in status.get("strategies", [])
                   if item.get("id") == "xiaomi_trend_v1"), None)
    if not xiaomi:
        return None
    action = str(xiaomi.get("raw_action") or xiaomi.get("action") or "").upper()
    if action not in {"BUY", "SELL"}:
        return None
    phase = execution_phase(now)
    as_of = str(xiaomi.get("as_of") or "unknown")
    qty = int(xiaomi.get("raw_suggested_qty") or xiaomi.get("suggested_qty")
              or xiaomi.get("held_qty") or 0)
    signal_price = float(xiaomi.get("price") or 0)
    portfolio = status.get("portfolio") or {}
    sizing = xiaomi.get("sizing") or {}
    cash = portfolio.get("cash")
    total_assets = portfolio.get("total_assets")
    price_for_funds = float(live_price or signal_price or 0)
    reference_required = qty * price_for_funds if action == "BUY" else 0.0
    affordable = cash is not None and float(cash) >= reference_required
    executable = action != "BUY" or (phase != "LATE_DO_NOT_CHASE" and affordable)
    current_action = action if executable else "WAIT"
    current_qty = qty if executable else 0
    required = current_qty * price_for_funds if current_action == "BUY" else 0.0
    funding = {
        "source": portfolio.get("funds_source"),
        "as_of": portfolio.get("funds_as_of"),
        "cash_hkd": cash,
        "total_assets_hkd": total_assets,
        "required_hkd": required,
        "reference_required_hkd": reference_required,
        "post_trade_cash_hkd": round(max(float(cash) - required, 0.0), 2) if cash is not None else None,
        "affordable": affordable if action == "BUY" else True,
        "matching_accounts": portfolio.get("matching_accounts"),
        "active_accounts": portfolio.get("active_accounts"),
    }
    drift_pct = ((float(live_price) / signal_price - 1) * 100
                 if live_price and signal_price else None)
    if action == "BUY":
        if not affordable:
            verdict = "富途实时可用现金不足，禁止执行买入"
        elif phase == "PREOPEN":
            verdict = f"开盘前计划：复核买入小米正股{qty}股"
        elif phase == "OPEN_WINDOW":
            verdict = f"开盘执行窗口：复核买入小米正股{qty}股"
        else:
            verdict = "原开盘买入窗口已过，不按盘中涨幅追价"
    else:
        verdict = f"退出信号：复核卖出小米现有{qty}股"
    lines = [
        f"**小米正式策略执行提醒｜{phase}**",
        verdict,
        f"信号日 {as_of}；信号价 HK${signal_price:.2f}",
    ]
    if live_price:
        lines.append(f"现价 HK${float(live_price):.2f}；偏离信号价 {drift_pct:+.2f}%")
    if cash is not None and total_assets is not None:
        if current_action == "WAIT" and action == "BUY":
            lines.append(
                f"富途实时资金：现金HK${float(cash):,.2f}／总资产HK${float(total_assets):,.2f}；"
                f"原信号参考{qty}股／约HK${reference_required:,.2f}，当前执行0股")
        else:
            lines.append(
                f"富途实时资金：现金HK${float(cash):,.2f}／总资产HK${float(total_assets):,.2f}；"
                f"本次预计HK${required:,.2f}／执行后现金HK${funding['post_trade_cash_hkd']:,.2f}")
    else:
        lines.append("富途实时资金不可用；禁止据此执行新买入")
    conflict = xiaomi.get("execution_conflict") or {}
    if conflict:
        current = conflict.get("current_delta_equivalent_shares")
        projected = conflict.get("projected_delta_equivalent_shares")
        if current is not None and projected is not None:
            lines.append(f"现有期权Delta约{current:.0f}股；加正股后组合约{projected:.0f}股")
    option = option_review or {}
    contract = option.get("contract") or {}
    if option:
        if option.get("action") == "REVIEW":
            lines.append(f"期权仅复核：{contract.get('code')}，最大权利金损失约HK${contract.get('max_loss_per_contract_hkd', 0):.0f}")
        else:
            budget = (option.get("gates") or {}).get("max_loss_budget_hkd")
            loss = contract.get("max_loss_per_contract_hkd")
            detail = f"一张约HK${loss:.0f}，预算HK${budget:.0f}" if loss and budget is not None else option.get("reason")
            lines.append(f"期权不合格：{detail}；不提示买入Call")
    lines.append("只读提醒，不自动下单。")
    return {
        "fingerprint": f"execution:xiaomi_trend_v1:{as_of}:{action}:{phase}",
        "title": "Contrarian 小米策略提醒",
        "message": "\n".join(lines),
        "phase": phase, "action": current_action, "raw_signal_action": action,
        "qty": current_qty, "raw_signal_qty": qty,
        "live_price": live_price, "signal_price": signal_price,
        "funding": funding,
    }

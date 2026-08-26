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
    drift_pct = ((float(live_price) / signal_price - 1) * 100
                 if live_price and signal_price else None)
    if action == "BUY":
        if phase == "PREOPEN":
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
        "phase": phase, "action": action, "qty": qty,
        "live_price": live_price, "signal_price": signal_price,
    }

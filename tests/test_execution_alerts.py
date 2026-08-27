from datetime import datetime

from app.modules import execution_alerts


def _status():
    return {"portfolio": {"cash": 20911.4, "total_assets": 33185.4,
                           "funds_source": "ALL_MATCHING_HK_REAL_ACCOUNTS",
                           "matching_accounts": 2, "active_accounts": 1},
            "strategies": [{
        "id": "xiaomi_trend_v1", "action": "BUY", "as_of": "2026-08-25",
        "price": 27.76, "suggested_qty": 200,
        "execution_conflict": {"current_delta_equivalent_shares": -279,
                               "projected_delta_equivalent_shares": -79},
    }]}


def test_preopen_alert_contains_stock_plan_and_option_rejection():
    alert = execution_alerts.build(
        _status(), now=datetime(2026, 8, 26, 9, 20), live_price=27.90,
        option_review={"action": "BLOCKED", "contract": {"max_loss_per_contract_hkd": 1200},
                       "gates": {"max_loss_budget_hkd": 200}})
    assert alert["phase"] == "PREOPEN"
    assert "200股" in alert["message"]
    assert "期权不合格" in alert["message"]
    assert "HK$1200" in alert["message"]
    assert "现金HK$20,911.40" in alert["message"]
    assert alert["funding"]["post_trade_cash_hkd"] == 15331.4


def test_late_alert_explicitly_prevents_chasing():
    alert = execution_alerts.build(
        _status(), now=datetime(2026, 8, 26, 11, 0), live_price=28.86)
    assert alert["phase"] == "LATE_DO_NOT_CHASE"
    assert "不按盘中涨幅追价" in alert["message"]
    assert alert["action"] == "WAIT" and alert["qty"] == 0
    assert alert["raw_signal_action"] == "BUY" and alert["raw_signal_qty"] == 200
    assert "当前执行0股" in alert["message"]


def test_same_day_close_signal_waits_for_next_session():
    alert = execution_alerts.build(
        _status(), now=datetime(2026, 8, 25, 22, 0), live_price=27.76)
    assert alert["phase"] == "PENDING_NEXT_SESSION"
    assert "下一交易日开盘前复核" in alert["message"]
    assert "窗口已过" not in alert["message"]
    assert alert["action"] == "WAIT" and alert["qty"] == 0


def test_buy_alert_is_blocked_when_live_cash_is_insufficient():
    status = _status()
    status["portfolio"]["cash"] = 1000
    alert = execution_alerts.build(
        status, now=datetime(2026, 8, 26, 9, 35), live_price=28)
    assert "现金不足，禁止执行" in alert["message"]
    assert alert["funding"]["affordable"] is False

from datetime import datetime

from app.modules import execution_alerts


def _status():
    return {"strategies": [{
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


def test_late_alert_explicitly_prevents_chasing():
    alert = execution_alerts.build(
        _status(), now=datetime(2026, 8, 26, 11, 0), live_price=28.86)
    assert alert["phase"] == "LATE_DO_NOT_CHASE"
    assert "不按盘中涨幅追价" in alert["message"]

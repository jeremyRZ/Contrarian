import pandas as pd

from app.modules.xiaomi_directional import (DirectionalParams, current_signal,
                                            desired_state, notification)


def test_dual_ma_produces_long_flat_short_states():
    p = DirectionalParams("dual_ma", 20, 80, 0.02)
    assert desired_state(pd.Series({"ma20": 103, "ma80": 100}), p) == 1
    assert desired_state(pd.Series({"ma20": 101, "ma80": 100}), p) == 0
    assert desired_state(pd.Series({"ma20": 97, "ma80": 100}), p) == -1


def test_current_short_is_a_sell_alert_not_an_order():
    x = pd.DataFrame([{"time_key": pd.Timestamp("2026-08-21"), "close": 29,
                       "ma20": 90, "ma80": 100}])
    result = current_signal(x, DirectionalParams("dual_ma", 20, 80, 0.02))
    assert result["action"] == "SELL"
    assert "qty" not in result


def test_notifications_are_actionable_transitions_only():
    base = {"as_of": "2026-08-21", "price": 29.02, "momentum_20d_pct": 8.61,
            "threshold_pct": 5, "shortability": {"confirmed": False}}
    assert notification({**base, "transition": False, "action": "BUY"}) is None
    assert notification({**base, "transition": True, "action": "WAIT"}) is None
    fp, text = notification({**base, "transition": True, "action": "BUY"})
    assert fp.endswith(":BUY")
    assert "不会下单" in text


def test_short_alert_warns_when_borrow_is_unconfirmed():
    status = {"as_of": "2026-08-21", "price": 29.02, "momentum_20d_pct": -6,
              "threshold_pct": 5, "shortability": {"confirmed": False},
              "transition": True, "action": "SELL"}
    _, text = notification(status)
    assert "未确认券源" in text


def test_live_status_rejects_stale_market_data(monkeypatch):
    class Client:
        def history_kline(self, *args, **kwargs):
            dates = pd.date_range("2020-01-01", periods=30, freq="B")
            return pd.DataFrame({"time_key": dates, "close": range(30)}), None

    from app.modules import xiaomi_directional
    result, error = xiaomi_directional.live_status(Client(), {"xiaomi_directional": {}})
    assert result is None
    assert "过期" in error

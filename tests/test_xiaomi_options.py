import pandas as pd

from app.modules.xiaomi_options import (breakout_signal, momentum_supertrend_signal,
                                        notification, rank_contracts,
                                        rank_convex_contracts)


def _row(**overrides):
    row = {"code": "HK.TEST", "option_type": "CALL", "bid_price": 1.20,
           "ask_price": 1.30, "option_delta": 0.45,
           "option_implied_volatility": 45, "option_strike_price": 30,
           "option_theta": -0.02, "option_vega": 0.03,
           "option_open_interest": 2000, "volume": 200, "lot_size": 1000}
    row.update(overrides)
    return row


def test_rank_contracts_enforces_liquidity_delta_and_iv():
    frame = pd.DataFrame([_row(), _row(code="WIDE", ask_price=1.80),
                          _row(code="RICH", option_implied_volatility=80)])
    result = rank_contracts(frame, "CALL", realized_vol_pct=50)
    assert [x["code"] for x in result] == ["HK.TEST"]


def test_put_uses_negative_delta():
    frame = pd.DataFrame([_row(code="PUT", option_type="PUT", option_delta=-0.45)])
    assert rank_contracts(frame, "PUT", realized_vol_pct=50)[0]["side"] == "PUT"


def test_notification_requires_qualified_option():
    result = {"as_of": "2026-08-21", "instrument": "STOCK",
              "underlying": {"transition": True}, "contract": None}
    assert notification(result) is None


def test_research_option_cannot_notify_even_with_contract():
    result = {"instrument": "PUT", "contract": {"code": "HK.TEST"}}
    assert notification(result) is None


def test_breakout_signal_uses_only_prior_55_days():
    bars = pd.DataFrame({"time_key": pd.date_range("2024-01-01", periods=56),
                         "high": [10] * 55 + [12], "low": [9] * 56,
                         "close": [9.5] * 55 + [11]})
    assert breakout_signal(bars)["action"] == "BUY_CALL"


def test_convex_ranker_targets_otm_strike():
    frame = pd.DataFrame([_row(code="NEAR", option_strike_price=33, option_delta=.30),
                          _row(code="FAR", option_strike_price=40, option_delta=.05)])
    assert rank_convex_contracts(frame, "CALL", 30, target_otm_pct=10)[0]["code"] == "NEAR"


def test_convex_ranker_rejects_lottery_delta_and_far_strike():
    frame = pd.DataFrame([_row(code="LOTTERY", option_strike_price=38, option_delta=.07)])
    assert rank_convex_contracts(frame, "CALL", 29.02, target_otm_pct=15) == []


def test_momentum_supertrend_requires_new_confirmed_state(monkeypatch):
    bars = pd.DataFrame({"time_key": pd.date_range("2025-01-01", periods=30, freq="B"),
                         "open": [10] * 30, "high": [11] * 30, "low": [9] * 30,
                         "close": [10] * 29 + [11]})
    monkeypatch.setattr("app.modules.xiaomi_options.supertrend", lambda *_args, **_kwargs:
                        pd.DataFrame({"st_direction": [1] * 30}))
    assert momentum_supertrend_signal(bars)["action"] == "BUY_CALL"


def test_momentum_supertrend_rejects_unconfirmed_direction(monkeypatch):
    bars = pd.DataFrame({"time_key": pd.date_range("2025-01-01", periods=30, freq="B"),
                         "open": [10] * 30, "high": [11] * 30, "low": [9] * 30,
                         "close": [10] * 29 + [11]})
    monkeypatch.setattr("app.modules.xiaomi_options.supertrend", lambda *_args, **_kwargs:
                        pd.DataFrame({"st_direction": [-1] * 30}))
    assert momentum_supertrend_signal(bars)["action"] == "WAIT"

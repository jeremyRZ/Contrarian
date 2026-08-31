import json

import pandas as pd
from datetime import datetime
import pytest

from app.modules import strategy_center


@pytest.fixture(autouse=True)
def hk_calendar_cache(tmp_path, monkeypatch):
    sessions = {str(day.date()): "WHOLE"
                for day in pd.date_range("2020-01-01", "2030-12-31", freq="B")}
    path = tmp_path / "hk_calendar.json"
    path.write_text(json.dumps({"sessions": sessions}), encoding="utf-8")
    monkeypatch.setattr(strategy_center.hk_calendar, "CALENDAR_FILE", path)


def test_strategy_center_has_no_order_placement_api():
    assert not hasattr(strategy_center, "place_order")
    assert not hasattr(strategy_center, "submit_order")


def test_all_production_strategy_contracts_load():
    for strategy_id in (
        "xiaomi_trend_v1",
        "hk_liquid_trend_rotation_v2",
        "hk_long_term_high_breakout_v1",
    ):
        assert strategy_center._strategy_contract(strategy_id)["strategy_id"] == strategy_id


def test_serial_rejects_plain_python_nan():
    assert strategy_center._serial(float("nan")) is None
    assert strategy_center._serial(float("inf")) is None
    assert strategy_center._serial(1.25) == 1.25
    assert strategy_center._serial(strategy_center.date(2026, 8, 27)) == "2026-08-27"


def test_positions_degrades_to_empty_on_error():
    class Broken:
        def positions(self):
            raise RuntimeError("no trade context")

    assert strategy_center._positions(Broken()) == {}


def test_strategy_positions_prefer_all_hk_accounts():
    class Client:
        def positions_market(self, market):
            assert market == "HK"
            return pd.DataFrame([
                {"code": "HK.01810", "qty": 200},
                {"code": "HK.01810", "qty": 400},
            ]), None
        def positions(self):
            raise AssertionError("default account must not be used")
    assert strategy_center._positions(Client()) == {"HK.01810": 600.0}


def _fresh_funds(cash=10000, total_assets=20000):
    return {"available_cash": cash, "total_assets": total_assets,
            "funds_complete": True, "funds_as_of": datetime.now().isoformat()}


def test_read_daily_deduplicates_latest_bar(tmp_path):
    p = tmp_path / "bars.csv"
    pd.DataFrame({"time_key": ["2026-08-12", "2026-08-12"], "close": [1, 2]}).to_csv(p, index=False)
    result = strategy_center._read_daily(p)
    assert len(result) == 1
    assert result.iloc[0].close == 2


def test_read_daily_accepts_mixed_futu_date_and_datetime_values(tmp_path):
    p = tmp_path / "bars.csv"
    pd.DataFrame({
        "time_key": ["2026-08-13", "2026-08-14 00:00:00"],
        "close": [1, 2],
    }).to_csv(p, index=False)

    result = strategy_center._read_daily(p)

    assert list(result["close"]) == [1, 2]


def test_xiaomi_trailing_high_persists_and_triggers_at_fifteen_pct(tmp_path, monkeypatch):
    state_file = tmp_path / "xiaomi_state.json"
    monkeypatch.setattr(strategy_center, "XIAOMI_POSITION_STATE_FILE", state_file)
    first = strategy_center._xiaomi_trailing_state(
        held=True, bar_date="2026-08-25", high=30, close=29)
    second = strategy_center._xiaomi_trailing_state(
        held=True, bar_date="2026-08-26", high=32, close=27)
    assert first["peak_high"] == 30
    assert second["peak_high"] == 32
    assert round(second["drawdown_pct"], 3) == -15.625
    assert second["triggered"] is True


def test_xiaomi_trailing_state_closes_when_position_disappears(tmp_path, monkeypatch):
    state_file = tmp_path / "xiaomi_state.json"
    monkeypatch.setattr(strategy_center, "XIAOMI_POSITION_STATE_FILE", state_file)
    strategy_center._xiaomi_trailing_state(
        held=True, bar_date="2026-08-25", high=30, close=29)
    result = strategy_center._xiaomi_trailing_state(
        held=False, bar_date="2026-08-26", high=31, close=30)
    assert result["active"] is False
    assert json.loads(state_file.read_text(encoding="utf-8"))["active"] is False


def test_breakout_strategy_is_exposed_read_only(monkeypatch):
    monkeypatch.setattr(strategy_center, "_cache_needs_refresh", lambda: False)
    monkeypatch.setattr(strategy_center, "_data_freshness", lambda: {"status": "CURRENT",
        "latest_date": "2026-08-24", "expected_through": "2026-08-24",
        "refresh_attempted": False, "note": None})
    monkeypatch.setattr(strategy_center, "_positions", lambda client: {})
    monkeypatch.setattr(strategy_center, "_xiaomi_status", lambda p: {"id": "x"})
    monkeypatch.setattr(strategy_center, "_rotation_status", lambda p: {"id": "r"})
    monkeypatch.setattr(strategy_center, "_breakout_status", lambda p: {"id": "b", "action": "WAIT"})
    result = strategy_center.get_status(object(), refresh=False)
    assert [x["id"] for x in result["strategies"]] == ["x", "r", "b"]


def test_cache_refreshes_when_latest_completed_session_is_missing(tmp_path, monkeypatch):
    daily = tmp_path / "daily"
    daily.mkdir()
    pd.DataFrame({"time_key": ["2026-08-12"]}).to_csv(daily / "HK_800000.csv", index=False)
    meta = tmp_path / "refresh.json"
    monkeypatch.setattr(strategy_center, "DAILY_DIR", daily)
    monkeypatch.setattr(strategy_center, "REFRESH_META_FILE", meta)

    assert strategy_center._cache_needs_refresh(
        strategy_center.datetime(2026, 8, 25, 7, 30)) is True


def test_refresh_cache_fails_once_when_opend_is_down(tmp_path, monkeypatch):
    meta = tmp_path / "refresh.json"
    monkeypatch.setattr(strategy_center, "REFRESH_META_FILE", meta)
    monkeypatch.setattr(strategy_center, "DAILY_DIR", tmp_path / "daily")
    class Client:
        def connect(self): return False, "OpenD down"
        def stock_basicinfo(self): raise AssertionError("must not scan after failed preflight")
    assert strategy_center._refresh_cache(Client()) == ["OpenD down"]
    assert json.loads(meta.read_text(encoding="utf-8"))["errors"] == ["OpenD down"]


def test_refresh_cache_stops_after_three_consecutive_symbol_failures(tmp_path, monkeypatch):
    daily = tmp_path / "daily"
    universe = tmp_path / "universe.csv"
    meta = tmp_path / "refresh.json"
    pd.DataFrame([{"code": "HK.00700"}]).to_csv(universe, index=False)
    monkeypatch.setattr(strategy_center, "DAILY_DIR", daily)
    monkeypatch.setattr(strategy_center, "UNIVERSE_FILE", universe)
    monkeypatch.setattr(strategy_center, "REFRESH_META_FILE", meta)
    monkeypatch.setattr(strategy_center, "_ensure_universe", lambda client, force=True: [])
    class Client:
        calls = 0
        def connect(self): return True, "connected"
        def close(self): pass
        def history_kline(self, *args, **kwargs):
            self.calls += 1
            return None, "transport down"
    client = Client()
    errors = strategy_center._refresh_cache(client)
    assert client.calls == 6
    assert errors[-1] == "OpenD连续3个标的更新失败，本轮刷新已提前终止"


def test_preopen_expected_session_skips_weekend(tmp_path, monkeypatch):
    path = tmp_path / "calendar.json"
    path.write_text(json.dumps({"sessions": {"2026-08-21": "WHOLE"}}), encoding="utf-8")
    monkeypatch.setattr(strategy_center.hk_calendar, "CALENDAR_FILE", path)
    expected = strategy_center._expected_completed_session(
        strategy_center.datetime(2026, 8, 24, 8, 0))
    assert str(expected) == "2026-08-21"


def test_universe_payload_exposes_exact_scanned_stocks(tmp_path, monkeypatch):
    universe = tmp_path / "universe.csv"
    pd.DataFrame([
        {"code": "HK.00700", "name": "腾讯控股", "lot_size": 100},
        {"code": "HK.01810", "name": "小米集团-W", "lot_size": 200},
    ]).to_csv(universe, index=False)
    monkeypatch.setattr(strategy_center, "UNIVERSE_FILE", universe)

    result = strategy_center._universe_payload()

    assert result["count"] == 2
    assert [(x["code"], x["name"]) for x in result["stocks"]] == [
        ("HK.00700", "腾讯控股"),
        ("HK.01810", "小米集团-W"),
    ]
    assert all("sector" in x and "stage" in x for x in result["stocks"])


def test_rotation_keeps_stock_watch_candidates_when_market_gate_is_closed(tmp_path, monkeypatch):
    dates = pd.bdate_range(end="2026-08-26", periods=221)
    daily = tmp_path / "daily"; daily.mkdir()
    index = pd.DataFrame({
        "time_key": dates, "code": "HK.800000", "open": 100, "high": 101,
        "low": 98, "close": [100.0] * 220 + [99.0], "volume": 1_000_000,
        "turnover": 200_000_000,
    })
    stock = pd.DataFrame({
        "time_key": dates, "code": "HK.01398", "open": range(221),
        "high": [x + 1 for x in range(221)], "low": range(221),
        "close": pd.Series(range(221), dtype=float) + 10,
        "volume": 10_000_000, "turnover": 200_000_000,
    })
    index.to_csv(daily / "HK_800000.csv", index=False)
    stock.to_csv(daily / "HK_01398.csv", index=False)
    universe = tmp_path / "universe.csv"
    pd.DataFrame([{"code": "HK.01398", "name": "工商银行", "lot_size": 100}]).to_csv(
        universe, index=False)
    monkeypatch.setattr(strategy_center, "DAILY_DIR", daily)
    monkeypatch.setattr(strategy_center, "UNIVERSE_FILE", universe)
    monkeypatch.setattr(strategy_center.forward_ledger, "managed_codes", lambda *_: set())

    result = strategy_center._rotation_status({}, {"cash": 20_000, "total_assets": 20_000})

    assert result["market"]["eligible"] is False
    assert result["candidate_mode"] == "OBSERVE_ONLY"
    assert result["candidates"][0]["code"] == "HK.01398"
    assert result["candidates"][0]["suggested_qty"] == 0
    assert result["proposed"] == [] and result["orders"] == []


def test_portfolio_gate_blocks_negative_cash_and_concentration(monkeypatch):
    monkeypatch.setattr(strategy_center, "_risk_settings", lambda: {
        "portfolio_gate_enabled": True,
        "min_cash_pct": 15.0, "max_single_position_pct": 30.0,
        "max_leveraged_position_pct": 15.0, "max_trade_risk_pct": 1.0,
    })
    gate = strategy_center._portfolio_gate({
        "available": True, "cash_ratio": -10.0, "max_weight_pct": 60.0,
        "underlyings": [{"weight_pct": 25.0, "leveraged": True}],
    })
    assert gate["allow_new_risk"] is False
    assert len(gate["reasons"]) == 3


def test_risk_position_size_uses_equity_cash_and_stop(monkeypatch):
    monkeypatch.setattr(strategy_center, "_risk_settings", lambda: {
        "portfolio_gate_enabled": True,
        "min_cash_pct": 15.0, "max_single_position_pct": 30.0,
        "max_leveraged_position_pct": 15.0, "max_trade_risk_pct": 1.0,
    })
    result = strategy_center._risk_position_size(
        entry=10.0, stop=9.0, lot_size=100,
        portfolio=_fresh_funds(50_000, 100_000), max_position_pct=20.0)
    assert result["qty"] == 1000
    assert result["risk_hkd"] == 1000
    assert result["risk_pct"] == 1.0


def test_xiaomi_fixed_allocation_uses_frozen_contract_and_board_lot():
    result = strategy_center._fixed_allocation_size(
        entry=27.50, lot_size=200, portfolio=_fresh_funds(50_000),
        capital=20_000, allocation_pct=50.0)
    assert result["qty"] == 200
    assert result["estimated_amount_hkd"] == 5500
    assert result["allocation_budget_hkd"] == 10_000
    assert result["estimated_cost_hkd"] == 29.10
    assert result["post_trade_cash_hkd"] == 44_470.90
    assert result["affordable"] is True


def test_xiaomi_fixed_allocation_uses_live_cash_before_frozen_budget():
    result = strategy_center._fixed_allocation_size(
        entry=27.50, lot_size=200,
        portfolio={**_fresh_funds(5000, 8000),
                   "funds_source": "ALL_MATCHING_HK_REAL_ACCOUNTS"},
        capital=20_000, allocation_pct=50.0)
    assert result["qty"] == 0
    assert result["available_cash_hkd"] == 5000
    assert result["affordable"] is False


def test_portfolio_gate_converts_buy_to_blocked():
    strategies = [{"action": "BUY", "suggested_qty": 100,
                   "candidates": [{"suggested_qty": 100}]}]
    strategy_center._apply_portfolio_gate(
        strategies, {"allow_new_risk": False, "reasons": ["现金不足"]})
    assert strategies[0]["action"] == "BLOCKED"
    assert strategies[0]["suggested_qty"] == 0
    assert strategies[0]["candidates"][0]["suggested_qty"] == 0


def test_existing_xiaomi_put_discloses_exposure_without_overriding_stock_signal():
    strategies = [{"id": "xiaomi_trend_v1", "action": "BUY", "price": 25.0,
                   "suggested_qty": 200, "reason": "趋势成立"}]
    portfolio = {"underlyings": [{
        "code": "HK.01810", "delta_exposure_available": True,
        "estimated_directional_exposure_hkd": -7000,
        "derivatives": [{"code": "HK.MIU260929P26000"}],
    }]}

    strategy_center._apply_execution_conflicts(strategies, portfolio)

    assert strategies[0]["action"] == "BUY"
    assert strategies[0]["suggested_qty"] == 200
    assert strategies[0]["execution_conflict"]["blocking"] is False
    assert strategies[0]["execution_conflict"]["current_delta_equivalent_shares"] == -280
    assert strategies[0]["execution_conflict"]["projected_delta_equivalent_shares"] == -80


def test_execution_overlap_and_buy_are_both_visible_in_action_queue():
    conflict = {"type": "EXISTING_DERIVATIVE_EXPOSURE"}
    strategies = [{"id": "xiaomi_trend_v1", "name": "小米专属趋势", "price": 25,
                   "action": "BUY", "suggested_qty": 200, "reason": "期权方向重叠",
                   "execution_conflict": conflict, "trade_plan": {"trigger": 25}}]
    queue = strategy_center._action_queue(
        {"underlyings": [], "gate": {"allow_new_risk": True}}, strategies)
    assert [item["action"] for item in queue] == ["方向敞口复核", "买入复核"]
    assert all(item["allowed"] is True for item in queue)


def test_xiaomi_next_open_entry_expires_after_ten_without_erasing_signal():
    strategies = [{"id": "xiaomi_trend_v1", "action": "BUY", "as_of": "2026-08-25",
                   "suggested_qty": 200, "reason": "趋势成立"}]
    strategy_center._apply_execution_timing(
        strategies, strategy_center.datetime(2026, 8, 26, 10, 1))
    assert strategies[0]["action"] == "WAIT"
    assert strategies[0]["raw_action"] == "BUY"
    assert strategies[0]["suggested_qty"] == 0
    assert strategies[0]["raw_suggested_qty"] == 200
    assert strategies[0]["execution_status"] == "MISSED_NEXT_OPEN_WINDOW"


def test_xiaomi_entry_remains_actionable_during_open_window():
    strategies = [{"id": "xiaomi_trend_v1", "action": "BUY", "as_of": "2026-08-25",
                   "suggested_qty": 200}]
    strategy_center._apply_execution_timing(
        strategies, strategy_center.datetime(2026, 8, 26, 9, 45))
    assert strategies[0]["action"] == "BUY"
    assert strategies[0]["suggested_qty"] == 200


def test_disabled_portfolio_gate_keeps_risk_metrics_advisory(monkeypatch):
    monkeypatch.setattr(strategy_center, "_risk_settings", lambda: {
        "portfolio_gate_enabled": False,
        "min_cash_pct": 15.0, "max_single_position_pct": 30.0,
        "max_leveraged_position_pct": 15.0, "max_trade_risk_pct": 1.0,
    })
    gate = strategy_center._portfolio_gate({
        "available": True, "cash_ratio": -10.0, "max_weight_pct": 60.0,
        "underlyings": [{"weight_pct": 25.0, "leveraged": True}],
    })
    assert gate["allow_new_risk"] is True
    assert gate["disabled"] is True
    assert gate["reasons"] == []


def test_action_queue_includes_single_stock_xiaomi_buy():
    portfolio = {"cash_ratio": 30, "underlyings": [],
                 "gate": {"allow_new_risk": True, "settings": {"min_cash_pct": 15}}}
    strategies = [{"id": "xiaomi_trend_v1", "action": "BUY",
                   "name": "小米专属趋势", "reason": "趋势成立", "price": 27.82,
                   "suggested_qty": 200, "sizing": {"risk_pct": 0.9},
                   "trade_plan": {"stop": 26.36}}]

    queue = strategy_center._action_queue(portfolio, strategies)

    assert queue[0]["code"] == "HK.01810"
    assert queue[0]["suggested_qty"] == 200
    assert queue[0]["estimated_amount"] == 5564


def test_position_stop_alert_overrides_generic_concentration_label():
    portfolio = {"underlyings": [{"code": "HK.08305", "name": "圣唐控股",
                                   "risk": "正常", "weight_pct": 12.2}]}
    scan = {"positions": [{"code": "HK.08305", "pl_ratio": -39.1,
                            "stop_loss_price": .085, "stop_pct": 8,
                            "signals": ["⚠️ 触及止损线"], "advice": "建议止损离场",
                            "lots": 70}]}

    strategy_center._merge_position_risk(portfolio, scan)

    assert portfolio["underlyings"][0]["risk"] == "止损触发"
    assert portfolio["underlyings"][0]["risk_severity"] == "DANGER"
    assert portfolio["risk_alert_count"] == 1


def test_disabled_portfolio_gate_demotes_concentration_to_advisory():
    portfolio = {"cash_ratio": 30, "underlyings": [
        {"code": "HK.02706", "name": "海致科技", "risk": "集中度过高",
         "weight_pct": 32.7, "leveraged": False, "derivatives": []}],
        "gate": {"allow_new_risk": True, "disabled": True,
                 "settings": {"min_cash_pct": 15}}}

    queue = strategy_center._action_queue(portfolio, [])

    assert queue[0]["level"] == "SHOULD"
    assert queue[0]["action"] == "降低集中度"

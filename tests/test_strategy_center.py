import json

import pandas as pd

from app.modules import strategy_center


def test_strategy_center_has_no_order_placement_api():
    assert not hasattr(strategy_center, "place_order")
    assert not hasattr(strategy_center, "submit_order")


def test_capability_roadmap_separates_running_learning_and_next(tmp_path, monkeypatch):
    candidate = tmp_path / "candidate.json"
    candidate.write_text('{"historical_2022_2026":{"net_return_pct":61.4,"profit_factor":2.61}}',
                         encoding="utf-8")
    monkeypatch.setattr(strategy_center, "CANDIDATE_RESULT_FILE", candidate)
    roadmap = strategy_center._capability_roadmap([
        {"id": "s1", "name": "策略一", "action": "WAIT",
         "validation": {"return_pct": 12.3}},
    ])
    assert roadmap["active"][0]["state"] == "RUNNING"
    assert roadmap["learning"][0]["state"] == "VALIDATING"
    assert "61.40%" in roadmap["learning"][0]["evidence"]
    assert [item["order"] for item in roadmap["next"]] == [1, 2, 3, 4]


def test_serial_rejects_plain_python_nan():
    assert strategy_center._serial(float("nan")) is None
    assert strategy_center._serial(float("inf")) is None
    assert strategy_center._serial(1.25) == 1.25


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


def test_preopen_expected_session_skips_weekend():
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
        portfolio={"total_assets": 100_000, "cash": 50_000}, max_position_pct=20.0)
    assert result["qty"] == 1000
    assert result["risk_hkd"] == 1000
    assert result["risk_pct"] == 1.0


def test_xiaomi_fixed_allocation_uses_frozen_contract_and_board_lot():
    result = strategy_center._fixed_allocation_size(
        entry=27.50, lot_size=200, portfolio={"cash": 50_000},
        capital=20_000, allocation_pct=50.0)
    assert result["qty"] == 200
    assert result["estimated_amount_hkd"] == 5500
    assert result["allocation_budget_hkd"] == 10_000
    assert result["post_trade_cash_hkd"] == 44_500
    assert result["affordable"] is True


def test_xiaomi_fixed_allocation_uses_live_cash_before_frozen_budget():
    result = strategy_center._fixed_allocation_size(
        entry=27.50, lot_size=200,
        portfolio={"cash": 5000, "total_assets": 8000,
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

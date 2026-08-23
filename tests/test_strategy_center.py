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


def test_breakout_strategy_is_exposed_read_only(monkeypatch):
    monkeypatch.setattr(strategy_center, "_positions", lambda client: {})
    monkeypatch.setattr(strategy_center, "_xiaomi_status", lambda p: {"id": "x"})
    monkeypatch.setattr(strategy_center, "_rotation_status", lambda p: {"id": "r"})
    monkeypatch.setattr(strategy_center, "_breakout_status", lambda p: {"id": "b", "action": "WAIT"})
    result = strategy_center.get_status(object(), refresh=False)
    assert [x["id"] for x in result["strategies"]] == ["x", "r", "b"]


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
        "min_cash_pct": 15.0, "max_single_position_pct": 30.0,
        "max_leveraged_position_pct": 15.0, "max_trade_risk_pct": 1.0,
    })
    result = strategy_center._risk_position_size(
        entry=10.0, stop=9.0, lot_size=100,
        portfolio={"total_assets": 100_000, "cash": 50_000}, max_position_pct=20.0)
    assert result["qty"] == 1000
    assert result["risk_hkd"] == 1000
    assert result["risk_pct"] == 1.0


def test_portfolio_gate_converts_buy_to_blocked():
    strategies = [{"action": "BUY", "suggested_qty": 100,
                   "candidates": [{"suggested_qty": 100}]}]
    strategy_center._apply_portfolio_gate(
        strategies, {"allow_new_risk": False, "reasons": ["现金不足"]})
    assert strategies[0]["action"] == "BLOCKED"
    assert strategies[0]["suggested_qty"] == 0
    assert strategies[0]["candidates"][0]["suggested_qty"] == 0

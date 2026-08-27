from app.modules import strategy_center


CFG = {"trailing_activation_profit_pct": 10, "trailing_drawdown_pct": 10,
       "maximum_hold_bars": 40}


def position(**updates):
    base = {"entry_price": 100, "initial_stop": 90, "peak_price": 100,
            "bars_held": 0, "last_bar_date": "2026-01-01"}
    return {**base, **updates}


def test_breakout_initial_stop_and_ma20_exit():
    _, reason = strategy_center._advance_breakout_position(
        position(), {"date": "2026-01-02", "high": 101, "low": 89, "close": 96}, 95, CFG)
    assert reason == "INITIAL_STOP"
    _, reason = strategy_center._advance_breakout_position(
        position(), {"date": "2026-01-02", "high": 101, "low": 96, "close": 94}, 95, CFG)
    assert reason == "CLOSE_BELOW_MA20"


def test_breakout_trailing_and_max_hold_exit():
    state, reason = strategy_center._advance_breakout_position(
        position(peak_price=115),
        {"date": "2026-01-02", "high": 115, "low": 103, "close": 106}, 95, CFG)
    assert state["trailing_active"] is True and reason == "TRAILING_STOP"
    _, reason = strategy_center._advance_breakout_position(
        position(bars_held=39),
        {"date": "2026-01-02", "high": 102, "low": 96, "close": 100}, 95, CFG)
    assert reason == "MAX_HOLD_40D"


def test_register_breakout_fill_persists_ownership(tmp_path, monkeypatch):
    path = tmp_path / "breakout.json"
    monkeypatch.setattr(strategy_center, "BREAKOUT_POSITION_STATE_FILE", path)
    state = strategy_center.register_breakout_fill("HK.00700", 100, 500, 10, "2026-01-02")
    assert state["initial_stop"] == 480
    assert strategy_center._read_state(path)["HK.00700"]["book"] == "SIMULATED"

import copy

import pandas as pd

from app import api
from app.modules import price_alert, reverse_signals, screener, strategy_config


def test_evaluate_signals_allows_rsi2_to_be_disabled():
    cfg = copy.deepcopy(strategy_config.DEFAULT_STRATEGY_CONFIG)
    cfg["strategies"]["rsi2_connor"]["enabled"] = False

    result = screener.evaluate_signals({"price": 100.0}, cfg)

    assert result["reason_inputs"]["rsi2"] is None


def test_warn_price_is_a_downside_threshold_and_read_can_be_non_consuming():
    price_alert._FIRED.clear()
    cfg = {"warn_px": 95.0, "alarm_px": 90.0, "stop_px": 85.0, "tp_px": 120.0}

    fresh_above, active_above = price_alert._evaluate(
        "HK.00001", "sample", 100.0, 0.0, cfg, mark_fired=False
    )
    fresh_below, active_below = price_alert._evaluate(
        "HK.00001", "sample", 94.0, 0.0, cfg, mark_fired=False
    )

    assert fresh_above == active_above == []
    assert [item[1] for item in fresh_below] == ["warn"]
    assert price_alert._FIRED == {}


def test_save_config_merges_partial_update_with_persisted_config(tmp_path, monkeypatch):
    path = tmp_path / "strategies.yaml"
    monkeypatch.setattr(strategy_config, "STRATEGIES_PATH", str(path))
    strategy_config.save_config({"push": {"light": 8.0}})

    saved = strategy_config.save_config({"llm": {"enabled": False}})

    assert saved["push"]["light"] == 8.0
    assert saved["llm"]["enabled"] is False


def test_reset_config_removes_unknown_persisted_fields(tmp_path, monkeypatch):
    path = tmp_path / "strategies.yaml"
    monkeypatch.setattr(strategy_config, "STRATEGIES_PATH", str(path))
    strategy_config.save_config({"legacy_broken_field": {"undefined": 1}})

    reset = strategy_config.reset_config()

    assert "legacy_broken_field" not in reset


def test_screener_api_defaults_to_hang_seng_tech_index(monkeypatch):
    captured = {}

    def fake_screen(*args, **kwargs):
        captured.update(kwargs)
        return {"results": []}, None

    monkeypatch.setattr(api.screener, "screen", fake_screen)
    monkeypatch.setattr(api, "client", lambda: object())
    monkeypatch.setitem(api.CONFIG, "screener", {})

    api.get_screener()

    assert captured["hstech_code"] == "HK.800700"


def test_score_layers_do_not_exceed_ten_and_disclose_missing_data():
    row = {"score": 8.0}
    reverse = {
        "score": 7.0,
        "details": {
            "southbound": {"score": 2.0},
            "buyback": {"score": 1.0},
            "news": {"error": "unavailable", "score": 0.0},
            "capital_flow": {"score": 1.0},
            "valuation": {"score": 2.0},
            "institution": {"score": 1.0},
            "dividend": {"score": 0.0},
            "earnings": {"error": "unavailable", "score": 0.0},
        },
    }

    screener._apply_score_layers(row, reverse)

    assert row["technical_score"] == 8.0
    assert 0 <= row["mispricing_score"] <= 10
    assert 0 <= row["total_score"] <= 10
    assert row["data_confidence"] == 75.0
    assert row["data_status"]["available"] == 6
    assert row["quality_gate"]["status"] in {"pass", "unknown", "fail"}


def test_reverse_weights_change_the_effective_score():
    details = {"southbound": {"score": 2.0}, "news": {"score": 1.0}}
    cfg = copy.deepcopy(strategy_config.DEFAULT_STRATEGY_CONFIG)
    baseline = reverse_signals._weighted_reverse_score(details, cfg)
    cfg["reverse_weights"]["southbound"] = 0.0

    adjusted = reverse_signals._weighted_reverse_score(details, cfg)

    assert adjusted < baseline

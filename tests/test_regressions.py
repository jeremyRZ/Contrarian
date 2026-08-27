from app.modules import price_alert, reverse_signals


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


def test_reverse_weights_change_the_effective_score():
    details = {"southbound": {"score": 2.0}, "news": {"score": 1.0}}
    baseline = reverse_signals._weighted_reverse_score(details)
    cfg = {"reverse_weights": {**reverse_signals._REVERSE_WEIGHTS, "southbound": 0.0}}

    adjusted = reverse_signals._weighted_reverse_score(details, cfg)

    assert adjusted < baseline

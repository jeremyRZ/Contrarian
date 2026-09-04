from app.modules import signal_governance


def test_paper_validating_sell_is_exit_observation_but_not_actionable():
    strategy = {"id": "xiaomi_trend_v1", "action": "SELL",
                "lifecycle": "PAPER_VALIDATING"}

    signal_governance.annotate_production_strategy(strategy)

    assert strategy["decision_role"] == "PAPER_VALIDATING"
    assert strategy["observed_trade_intent"] == "EXIT_LONG"
    assert strategy["trade_intent"] == "NO_TRADE"
    assert strategy["actionable"] is False


def test_research_model_cannot_become_actionable_by_using_buy_word():
    strategy = {"id": "xiaomi_momentum_20d_v1", "action": "BUY"}

    signal_governance.annotate_production_strategy(strategy)

    assert strategy["decision_role"] == "RESEARCH"
    assert strategy["actionable"] is False
    assert strategy["decision_authority"] == "无交易决策权"


def test_rotation_and_breakout_have_no_production_authority():
    for strategy_id in ("hk_liquid_trend_rotation_v2", "hk_long_term_high_breakout_v1"):
        strategy = {"id": strategy_id, "action": "BUY", "lifecycle": "PAPER_VALIDATING"}
        signal_governance.annotate_production_strategy(strategy)
        assert strategy["decision_role"] == "PAPER_VALIDATING"
        assert strategy["actionable"] is False


def test_only_manually_production_strategy_can_generate_notification(monkeypatch):
    status = {
        "mode": "READ_ONLY_PAPER_ADVICE",
        "data_freshness": {"status": "CURRENT"},
        "strategies": [
            {"id": "xiaomi_momentum_20d_v1", "name": "研究动量", "action": "SELL",
             "as_of": "2026-08-25", "reason": "短期下跌"},
            {"id": "xiaomi_trend_v1", "name": "小米专属趋势", "action": "BUY",
             "lifecycle": "PRODUCTION",
             "as_of": "2026-08-25", "reason": "趋势成立", "suggested_qty": 200,
             "price": 27.76},
        ],
    }
    signal_governance.annotate_production_strategy(status["strategies"][1])

    messages = signal_governance.production_notifications(status)

    assert len(messages) == 1
    fingerprint, text = messages[0]
    assert fingerprint == "production:xiaomi_trend_v1:2026-08-25:BUY"
    assert "建议股数：200股" in text
    assert "研究动量" not in text


def test_stale_data_blocks_all_production_notifications():
    status = {"mode": "READ_ONLY_PAPER_ADVICE",
              "data_freshness": {"status": "STALE"},
              "strategies": [{"id": "xiaomi_trend_v1", "action": "BUY"}]}

    assert signal_governance.production_notifications(status) == []


def test_no_strategy_is_production_before_manual_promotion():
    summary = signal_governance.governance_summary(
        notification_configured=True,
        strategies=[{"id": "x", "lifecycle": "PAPER_VALIDATING"}])
    assert summary["production_strategy_ids"] == []
    assert set(summary["strategy_lifecycles"].values()) == {"PAPER_VALIDATING"}


def test_notification_governance_has_no_desktop_fallback():
    summary = signal_governance.governance_summary(notification_configured=False)
    assert summary["notification"] == {
        "channel": "WECOM", "configured": False, "wecom_configured": False,
        "local_fallback_enabled": False, "status": "NOT_CONFIGURED"}


def test_daily_watchlist_is_non_actionable_and_names_ranked_stocks():
    status = {"data_freshness": {"status": "CURRENT"}, "strategies": [{
        "id": "hk_liquid_trend_rotation_v2", "as_of": "2026-08-26",
        "market": {"eligible": False},
        "candidates": [{"code": "HK.01398", "name": "工商银行",
                        "momentum_pct": 38.78, "score": 1.557}],
    }]}
    messages = signal_governance.watchlist_notifications(status)
    assert len(messages) == 1
    assert "工商银行(HK.01398)" in messages[0][1]
    assert "仅作观察，不执行买入" in messages[0][1]
    assert "不是交易指令" in messages[0][1]


def test_daily_watchlist_includes_breakout_rejection_and_funds_reference():
    status = {"data_freshness": {"status": "CURRENT"}, "strategies": [
        {"id": "hk_liquid_trend_rotation_v2", "as_of": "2026-09-04",
         "next_review_date": "约7个交易日后",
         "market": {"eligible": False, "distance_to_ma_pct": -.04},
         "candidates": [{"code": "HK.01398", "name": "工商银行", "price": 7.7,
                         "momentum_pct": 34, "score": 1.3, "reference_qty": 1000}]},
        {"id": "hk_long_term_high_breakout_v1", "candidates": [
            {"code": "HK.02556", "name": "迈富时", "volume_ratio": 1.55,
             "reference_qty": 0, "sizing": {"reason": "一手风险超预算"}}]},
    ]}
    text = signal_governance.watchlist_notifications(status)[0][1]
    assert "资金参考1000股/HK$7,700" in text
    assert "突破候选：迈富时(HK.02556)" in text
    assert "一手风险超预算" in text

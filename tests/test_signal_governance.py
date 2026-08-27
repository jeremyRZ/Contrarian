from app.modules import signal_governance


def test_sell_is_unambiguously_exit_long_not_open_short():
    strategy = {"id": "xiaomi_trend_v1", "action": "SELL"}

    signal_governance.annotate_production_strategy(strategy)

    assert strategy["decision_role"] == "PRODUCTION"
    assert strategy["trade_intent"] == "EXIT_LONG"
    assert strategy["actionable"] is True


def test_research_model_cannot_become_actionable_by_using_buy_word():
    strategy = {"id": "xiaomi_momentum_20d_v1", "action": "BUY"}

    signal_governance.annotate_production_strategy(strategy)

    assert strategy["decision_role"] == "RESEARCH_ONLY"
    assert strategy["actionable"] is False
    assert strategy["decision_authority"] == "无交易决策权"


def test_only_production_strategy_can_generate_notification():
    status = {
        "mode": "READ_ONLY_PAPER_ADVICE",
        "data_freshness": {"status": "CURRENT"},
        "strategies": [
            {"id": "xiaomi_momentum_20d_v1", "name": "研究动量", "action": "SELL",
             "as_of": "2026-08-25", "reason": "短期下跌"},
            {"id": "xiaomi_trend_v1", "name": "小米专属趋势", "action": "BUY",
             "as_of": "2026-08-25", "reason": "趋势成立", "suggested_qty": 200,
             "price": 27.76},
        ],
    }

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


def test_notification_governance_has_no_desktop_fallback():
    summary = signal_governance.governance_summary(notification_configured=False)
    assert summary["notification"] == {
        "channel": "WECOM", "configured": False, "wecom_configured": False,
        "local_fallback_enabled": False, "status": "NOT_CONFIGURED"}

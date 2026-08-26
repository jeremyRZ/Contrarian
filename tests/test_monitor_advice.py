from app.modules import monitor


def test_one_lot_take_profit_does_not_recommend_fractional_sale():
    advice = monitor._advice("正股(趋势)", 15, 8, 1,
                             [{"pct": 10, "action": "减仓1/4，止损上移至成本价"}])

    assert "仅1手无法分批" in advice
    assert "整手止盈" in advice

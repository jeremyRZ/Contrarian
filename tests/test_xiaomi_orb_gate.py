def test_live_gate_rejects_small_or_unprofitable_test_sample():
    def passed(trades, expectancy, pf, drawdown):
        return trades >= 30 and expectancy > 0 and pf >= 1.2 and drawdown >= -8

    assert not passed(17, -0.047, 0.855, -4.19)
    assert not passed(17, 0.2, 1.5, -2)
    assert not passed(40, -0.01, 1.5, -2)
    assert passed(40, 0.1, 1.3, -5)

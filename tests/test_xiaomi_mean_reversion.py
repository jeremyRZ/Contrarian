import pandas as pd

from app.modules.xiaomi_mean_reversion import MeanReversionParams, evaluate, signal


def test_long_and_short_signals_are_directional():
    oversold = pd.Series({"close": 90, "ma200": 80, "ma50": 95, "rsi2": 4, "z20": -2})
    overbought = pd.Series({"close": 110, "ma200": 120, "ma50": 105, "rsi2": 96, "z20": 2})
    assert signal(oversold, MeanReversionParams("long", 5, 1.5, 3, 5, "up"))
    assert signal(overbought, MeanReversionParams("short", 5, 1.5, 3, 5, "down"))


def test_short_signal_requires_requested_regime():
    row = pd.Series({"close": 110, "ma200": 100, "ma50": 105, "rsi2": 99, "z20": 2.5})
    assert not signal(row, MeanReversionParams("short", 5, 2, 3, 5, "down"))


def test_backtest_enters_on_next_open_and_charges_short_borrow():
    dates = pd.date_range("2026-01-01", periods=5, freq="D")
    x = pd.DataFrame({"time_key": dates, "open": [10, 10, 10, 9, 9],
                      "high": [10, 10, 10, 9, 9], "low": [10, 10, 9, 9, 9],
                      "close": [10, 10, 10, 9, 9], "ma5": [9, 9, 9, 9.5, 9.5],
                      "ma50": [11] * 5, "ma200": [11] * 5,
                      "rsi2": [99, 50, 50, 50, 50], "z20": [3, 0, 0, 0, 0]})
    p = MeanReversionParams("short", 5, 2, 3, 20, "down")
    with_borrow = evaluate(x, p, fee_bps=0, slippage_bps=0, annual_borrow_pct=8)
    without_borrow = evaluate(x, p, fee_bps=0, slippage_bps=0, annual_borrow_pct=0)
    assert with_borrow["trades"][0]["entry_date"] == "2026-01-02"
    assert with_borrow["return_pct"] < without_borrow["return_pct"]

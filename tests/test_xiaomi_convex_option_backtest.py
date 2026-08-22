import pandas as pd

from app.modules.xiaomi_convex_option_backtest import build_convex_signals, metrics


def test_signal_entry_is_strictly_after_signal_day():
    dates = pd.date_range("2024-01-01", periods=70, freq="B")
    close = [10.0] * 60 + [12.0] * 10
    stock = pd.DataFrame({"time_key": dates, "open": close, "high": close,
                          "low": close, "close": close})
    signals = build_convex_signals(stock)
    assert signals
    assert all(pd.Timestamp(s.entry_date) > pd.Timestamp(s.signal_date) for s in signals)


def test_metrics_preserve_convex_winner_and_bounded_loss():
    result = metrics([{"return_pct": -100}, {"return_pct": 515}])
    assert result["max_trade_return_pct"] == 515
    assert result["min_trade_return_pct"] == -100
    assert result["portfolio_return_pct"] > 0

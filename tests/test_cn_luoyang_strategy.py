import pandas as pd
import pytest

from app.modules.cn_luoyang_strategy import LuoyangParams, close_signal, prepare_bars


def _row(**overrides):
    values = {"close": 12.0, "ma_fast": 11.0, "ma_regime": 10.0,
              "entry_level": 11.5, "exit_level": 9.5, "atr20": 0.5}
    values.update(overrides)
    return pd.Series(values)


def test_buy_requires_breakout_and_positive_long_term_regime():
    assert close_signal(_row(), False)["action"] == "BUY"
    assert close_signal(_row(ma_fast=9.0), False)["action"] == "WAIT"
    assert close_signal(_row(close=11.0), False)["action"] == "WAIT"


def test_sell_uses_the_tighter_of_hard_and_atr_stop():
    result = close_signal(_row(close=10.4), True, entry_price=12.0, peak_price=13.0,
                          params=LuoyangParams(atr_multiple=4, hard_stop_pct=.10))
    assert result["action"] == "SELL"
    # ATR stop (13 - 4 * 0.5 = 11) is tighter than the 10.8 hard stop.
    assert "11.00" in result["reason"]


def test_holding_requires_entry_and_peak_prices():
    with pytest.raises(ValueError, match="entry_price"):
        close_signal(_row(), True)


def test_levels_are_shifted_and_do_not_include_signal_day():
    dates = pd.date_range("2020-01-01", periods=205)
    frame = pd.DataFrame({"time_key": dates, "open": 10.0, "high": 10.0,
                          "low": 9.0, "close": 9.5, "volume": 1000})
    frame.loc[204, ["high", "close"]] = [20.0, 20.0]
    bars = prepare_bars(frame)
    assert bars.iloc[-1].entry_level == 10.0

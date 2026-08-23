import numpy as np
import pandas as pd

from app.modules.supertrend_research import (SuperTrendParams, combine_states,
                                             evaluate_positions, supertrend)


def bars(rows=80):
    close = pd.Series(np.linspace(10, 20, rows) + np.sin(np.arange(rows) / 3))
    return pd.DataFrame({"open": close.shift(1).fillna(close.iloc[0]),
                         "high": close + 0.5, "low": close - 0.5,
                         "close": close})


def test_supertrend_does_not_change_history_when_future_bar_is_appended():
    original = bars()
    extended = pd.concat((original, pd.DataFrame([{"open": 20, "high": 100,
                                                   "low": 1, "close": 90}])),
                         ignore_index=True)
    params = SuperTrendParams(10, 3.0)
    expected = supertrend(original, params)
    actual = supertrend(extended, params).iloc[:-1]
    pd.testing.assert_frame_equal(expected, actual)


def test_entry_confirmation_waits_for_agreement_but_does_not_force_exit():
    base = pd.Series([0, 1, 1, 1, 0])
    trend = pd.Series([-1, -1, 1, -1, -1])
    assert combine_states(base, trend, "entry_confirmation").tolist() == [0, 0, 1, 1, 0]


def test_exit_overlay_locks_out_reentry_until_base_regime_changes():
    base = pd.Series([1, 1, 1, 1, 0, 1])
    trend = pd.Series([1, 1, -1, 1, 1, 1])
    assert combine_states(base, trend, "exit_overlay").tolist() == [1, 1, 0, 0, 0, 1]


def test_evaluation_executes_close_signal_on_next_open():
    frame = pd.DataFrame({"open": [10.0, 10.0, 11.0, 11.0],
                          "high": [10, 10, 11, 11], "low": [10, 10, 11, 11],
                          "close": [10, 10, 11, 11]})
    result = evaluate_positions(frame, pd.Series([1, 1, 1, 1]), allocation=1,
                                fee_bps=0, slippage_bps=0, annual_borrow_pct=0)
    assert round(result["return_pct"], 6) == 10.0

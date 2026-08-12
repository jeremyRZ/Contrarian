from datetime import datetime, timedelta

import pandas as pd

from app.modules.orb_strategy import OrbParams, run_day


def _bars(closes, highs=None, lows=None, start="2026-08-03 09:30"):
    highs = highs or [x + 0.05 for x in closes]
    lows = lows or [x - 0.05 for x in closes]
    t0 = datetime.fromisoformat(start)
    return pd.DataFrame({
        "time_key": [t0 + timedelta(minutes=i) for i in range(len(closes))],
        "open": closes, "high": highs, "low": lows, "close": closes,
        "volume": 1_000_000, "turnover": 10_000_000,
    })


def test_entry_is_next_bar_after_second_confirmation():
    prices = [100.0] * 15 + [100.2, 100.25, 100.30] + [100.3] * 10
    highs = [100.05] * 15 + [x + 0.01 for x in prices[15:]]
    lows = [99.95] * 15 + [x - 0.01 for x in prices[15:]]
    p = OrbParams(buffer_bps=5, min_range_bps=5, max_range_bps=100,
                  fee_bps_per_side=0, slippage_bps_per_side=0,
                  min_net_reward_risk=0, max_entry_slippage_bps=30)
    result = run_day(_bars(prices, highs, lows), p, lot_size=100)
    assert result["traded"]
    assert result["signal_time"].endswith("09:46:00")
    assert result["entry_time"].endswith("09:47:00")


def test_same_bar_stop_and_target_uses_stop():
    prices = [100.0] * 15 + [100.2, 100.21, 100.22, 100.2]
    highs = [100.05] * 15 + [100.21, 100.22, 101.0, 100.3]
    lows = [99.95] * 15 + [100.19, 100.20, 99.0, 100.1]
    p = OrbParams(buffer_bps=5, min_range_bps=5, max_range_bps=100,
                  fee_bps_per_side=0, slippage_bps_per_side=0,
                  min_net_reward_risk=0, max_entry_slippage_bps=30)
    result = run_day(_bars(prices, highs, lows), p)
    assert result["traded"]
    assert result["exit_reason"] == "stop"


def test_quantity_rounds_down_and_never_exceeds_risk_budget():
    prices = [100.0] * 15 + [100.2, 100.21, 100.22] + [100.2] * 5
    p = OrbParams(buffer_bps=5, min_range_bps=5, max_range_bps=100,
                  fee_bps_per_side=0, slippage_bps_per_side=0,
                  min_net_reward_risk=0, max_entry_slippage_bps=30,
                  max_position_pct=1)
    result = run_day(_bars(prices), p, equity=1_000_000, lot_size=500)
    assert result["qty"] % 500 == 0
    assert result["qty"] * result["band"] <= 1_000_000 * p.risk_per_trade


def test_short_signal_is_rejected_without_borrowability():
    prices = [100.0] * 15 + [99.8, 99.79, 99.78] + [99.8] * 5
    p = OrbParams(allow_long=False, allow_short=True, buffer_bps=5,
                  min_range_bps=5, max_range_bps=100)
    result = run_day(_bars(prices), p, shortable=False)
    assert not result["traded"]
    assert result["skip_reason"] == "no_confirmed_breakout"

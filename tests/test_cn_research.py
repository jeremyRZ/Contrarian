from __future__ import annotations

import numpy as np
import pandas as pd

from app.modules import cn_research


def _bars(n=150):
    dates = pd.bdate_range("2025-01-01", periods=n)
    close = np.linspace(10, 12, n)
    volume = np.full(n, 1000.0)
    if n > 100:
        close[100] = 12.15; volume[100] = 3000
    if n > 101:
        stop = min(106, n)
        close[101:stop] = np.linspace(12.2, 12.5, stop - 101)
    if n > 106:
        close[106:] = np.linspace(11.0, 10.0, n - 106)
    return pd.DataFrame({"date": dates, "open": close + .01, "high": close * 1.002,
                         "low": close * .998, "close": close, "volume": volume})


def test_cn_backtest_fills_after_signal_and_respects_lot_size():
    report = cn_research.backtest(_bars())
    assert report["trades"]
    trade = report["trades"][0]
    assert trade["entry_date"] > "2025-01-01"
    assert trade["qty"] % 100 == 0
    assert report["assumptions"]["settlement"] == "T+1"


def test_unvalidated_observation_never_becomes_formal_buy():
    signal = cn_research.latest_signal("SH.603993", _bars(101),
                                       {"status": "RESEARCH_ONLY", "passed": False})
    assert signal["observed_action"] == "BUY"
    assert signal["action"] == "WAIT"


def test_short_history_cannot_pass_validation():
    result = cn_research.validate(_bars())
    assert result["passed"] is False
    assert result["status"] == "RESEARCH_ONLY"


def test_drawdown_is_anchored_to_initial_equity():
    metrics = cn_research._metrics(pd.DataFrame({"net_return": [-.10, .20]}))
    assert metrics["max_drawdown_pct"] == -10.0

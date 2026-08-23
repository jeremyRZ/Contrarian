import pandas as pd
import pytest

from app.modules import strategy_portfolio


def test_risk_parity_reduces_weight_of_high_volatility_strategy():
    frame = pd.DataFrame({"low": [.01, -.01, .01, -.01, .01, -.01],
                          "high": [.04, -.04, .04, -.04, .04, -.04]})
    weights = strategy_portfolio.risk_parity_weights(frame)
    assert weights["low"] > weights["high"]
    assert sum(weights.values()) == pytest.approx(1, abs=1e-5)


def test_allocation_labels_fallback_as_collecting():
    result = strategy_portfolio.build_allocation([
        {"id": "a", "action": "WAIT", "validation": {"max_drawdown_pct": -10}},
        {"id": "b", "action": "WAIT", "validation": {"max_drawdown_pct": -20}},
    ], [])
    assert result["state"] == "COLLECTING"
    assert result["weights"]["a"] > result["weights"]["b"]

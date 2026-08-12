from datetime import datetime, timedelta

import pandas as pd

from app.modules.xiaomi_regime import eligible_dates


def _history(index_up=True):
    stock, index = [], []
    start = datetime(2026, 1, 5, 9, 30)
    for d in range(15):
        base = start + timedelta(days=d)
        for m in range(20):
            stock.append([base + timedelta(minutes=m), 10, 10.1, 9.9, 10, 100])
            ix_close = 100 + (m * .01 if index_up else -m * .01)
            index.append([base + timedelta(minutes=m), 100, 101, 99, ix_close, 100])
    cols = ["time_key", "open", "high", "low", "close", "volume"]
    return pd.DataFrame(stock, columns=cols), pd.DataFrame(index, columns=cols)


def test_market_regime_requires_index_confirmation():
    stock, up = _history(True)
    _, down = _history(False)
    assert eligible_dates(stock, up, opening_minutes=10)
    assert not eligible_dates(stock, down, opening_minutes=10)

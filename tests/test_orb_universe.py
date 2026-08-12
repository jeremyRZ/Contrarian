import pandas as pd

from app.modules.orb_universe import rank_hk_orb_candidates


def test_hk_universe_rejects_wide_spread_low_turnover_and_expensive_lot():
    rows = [
        ["HK.00700", 500, 499.8, 500, 2e9, 100],
        ["HK.00001", 10, 9.9, 10.1, 2e9, 100],
        ["HK.00002", 20, 19.99, 20, 1e6, 100],
        ["HK.00003", 1000, 999.5, 1000, 2e9, 500],
        ["US.AAPL", 200, 199.9, 200, 2e9, 1],
    ]
    df = pd.DataFrame(rows, columns=["code", "last_price", "bid_price",
                                     "ask_price", "turnover", "lot_size"])
    result = rank_hk_orb_candidates(df)
    assert result.code.tolist() == ["HK.00700"]

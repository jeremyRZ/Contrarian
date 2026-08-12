"""Hong Kong equity universe selection for the ORB strategy."""
from __future__ import annotations

import pandas as pd


# Liquid HK names used only as a seed when no user universe is supplied. The
# ranking below, not this order, decides which symbols enter the backtest.
HK_LIQUID_SEED = [
    "HK.00700", "HK.09988", "HK.03690", "HK.01024", "HK.01810",
    "HK.00941", "HK.01211", "HK.00981", "HK.09618", "HK.09888",
    "HK.00005", "HK.01299", "HK.02318", "HK.00388", "HK.00883",
    "HK.02020", "HK.06618", "HK.09626", "HK.01398", "HK.03988",
]


def rank_hk_orb_candidates(snapshot: pd.DataFrame, *, top_n: int = 15,
                           min_price: float = 2.0,
                           min_turnover_hkd: float = 100_000_000,
                           max_spread_bps: float = 20.0,
                           max_lot_notional_hkd: float = 100_000.0) -> pd.DataFrame:
    """Filter/rank HK stocks before expensive minute-history requests.

    Expected Futu-style columns: code, last_price, bid_price, ask_price,
    turnover, lot_size. Suspended/ETF/warrant rows can be excluded when the
    optional ``sec_type`` and ``suspension`` fields are available.
    """
    required = {"code", "last_price", "bid_price", "ask_price", "turnover", "lot_size"}
    missing = required.difference(snapshot.columns)
    if missing:
        raise ValueError("missing snapshot columns: " + ", ".join(sorted(missing)))
    df = snapshot.copy()
    for c in ("last_price", "bid_price", "ask_price", "turnover", "lot_size"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df[df.code.astype(str).str.startswith("HK.")]
    if "sec_type" in df:
        df = df[df.sec_type.astype(str).str.upper().isin({"STOCK", "SECURITYTYPE.STOCK"})]
    if "suspension" in df:
        df = df[~df.suspension.fillna(False).astype(bool)]
    mid = (df.bid_price + df.ask_price) / 2
    df["spread_bps"] = (df.ask_price - df.bid_price) / mid * 10_000
    df["lot_notional_hkd"] = df.last_price * df.lot_size
    df = df[(df.last_price >= min_price) & (df.bid_price > 0) &
            (df.ask_price >= df.bid_price) & (df.turnover >= min_turnover_hkd) &
            (df.spread_bps <= max_spread_bps) &
            (df.lot_notional_hkd <= max_lot_notional_hkd)]
    # Turnover rewards capacity; spread and board-lot notional penalise
    # friction/capital granularity. log keeps mega-caps from dominating.
    import numpy as np
    df["orb_universe_score"] = (np.log1p(df.turnover) -
                                np.log1p(df.spread_bps.clip(lower=.1)) -
                                .25 * np.log1p(df.lot_notional_hkd))
    return df.sort_values(["orb_universe_score", "turnover"], ascending=False).head(top_n)

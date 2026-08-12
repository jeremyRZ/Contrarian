"""Point-in-time regime filters for Xiaomi intraday ORB."""
from __future__ import annotations

import pandas as pd


def eligible_dates(stock: pd.DataFrame, index: pd.DataFrame, *,
                   opening_minutes: int = 15, volume_ratio: float = 1.0,
                   max_abs_gap_pct: float = 3.0,
                   min_index_open_return_bps: float = 0.0) -> set:
    """Return dates eligible using information known after the opening window.

    Opening-volume history is shifted one day, preventing today's data from
    influencing its own threshold. The index must be non-negative/strong over
    the same opening window for Xiaomi long entries.
    """
    s, idx = stock.copy(), index.copy()
    s["time_key"], idx["time_key"] = pd.to_datetime(s.time_key), pd.to_datetime(idx.time_key)
    rows = []
    previous_close = None
    for date, day in s.groupby(s.time_key.dt.date, sort=True):
        cont = day[(day.time_key.dt.time >= pd.Timestamp("09:30").time()) &
                   (day.time_key.dt.time < pd.Timestamp("12:00").time())]
        opening = cont.iloc[:opening_minutes]
        if len(opening) < opening_minutes:
            continue
        open_px = float(opening.iloc[0].open)
        gap = ((open_px / previous_close - 1) * 100) if previous_close else 0.0
        rows.append({"date": date, "opening_volume": float(opening.volume.sum()),
                     "gap_pct": gap})
        previous_close = float(day.iloc[-1].close)
    daily = pd.DataFrame(rows)
    if daily.empty:
        return set()
    daily["prior_opening_volume_median"] = daily.opening_volume.shift(1).rolling(
        20, min_periods=10).median()
    index_ret = {}
    for date, day in idx.groupby(idx.time_key.dt.date, sort=True):
        cont = day[(day.time_key.dt.time >= pd.Timestamp("09:30").time()) &
                   (day.time_key.dt.time < pd.Timestamp("12:00").time())]
        opening = cont.iloc[:opening_minutes]
        if len(opening) >= opening_minutes:
            index_ret[date] = (float(opening.iloc[-1].close) /
                               float(opening.iloc[0].open) - 1) * 10_000
    daily["index_open_return_bps"] = daily.date.map(index_ret)
    mask = ((daily.opening_volume >= daily.prior_opening_volume_median * volume_ratio) &
            (daily.gap_pct.abs() <= max_abs_gap_pct) &
            (daily.index_open_return_bps >= min_index_open_return_bps))
    return set(daily.loc[mask, "date"])


def filter_days(frame: pd.DataFrame, dates: set) -> pd.DataFrame:
    ts = pd.to_datetime(frame.time_key)
    return frame[ts.dt.date.isin(dates)].copy()

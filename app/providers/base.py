from __future__ import annotations

from typing import Protocol

import pandas as pd


class MarketDataProvider(Protocol):
    name: str
    def snapshot(self, codes: list[str]) -> tuple[pd.DataFrame | None, str | None]: ...
    def daily_bars(self, code: str, *, start: str | None = None,
                   end: str | None = None, count: int = 500) -> tuple[pd.DataFrame | None, str | None]: ...
    def securities(self, market: str) -> tuple[pd.DataFrame | None, str | None]: ...
    def positions(self, market: str | None = None) -> tuple[pd.DataFrame | None, str | None]: ...

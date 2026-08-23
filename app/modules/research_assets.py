"""Read-only status for assets that have not entered the live portfolio."""
from __future__ import annotations

from datetime import datetime, timezone


def status() -> dict:
    """Return honest observation status without making network requests."""
    now = datetime.now(timezone.utc)
    return {
        "as_of": now.isoformat(),
        "assets": [
            {
                "asset": "BTC",
                "stage": "WATCH",
                "stage_label": "观察中",
                "execution_mode": "NO_TRADING_CONNECTION",
                "included_in_portfolio": False,
                "data_state": "PROVIDER_NOT_CONFIGURED",
                "message": "尚未配置 BTC 行情与持仓数据源",
            },
        ],
    }

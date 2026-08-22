"""Read-only status for assets that have not entered the live portfolio."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
POLYMARKET_OBSERVATIONS = ROOT / "data" / "polymarket_observations.jsonl"


def _decimal(value) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _latest_valid_observation(path: Path) -> dict | None:
    if not path.exists():
        return None
    latest = None
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            try:
                item = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(item, dict) and item.get("observed_at"):
                latest = item
    return latest


def _is_stale(value: str, now: datetime, max_age_hours: int = 24) -> bool:
    try:
        observed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return True
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=timezone.utc)
    return (now - observed.astimezone(timezone.utc)).total_seconds() > max_age_hours * 3600


def status(observation_path: Path | None = None) -> dict:
    """Return honest observation status without making network requests."""
    now = datetime.now(timezone.utc)
    latest = _latest_valid_observation(observation_path or POLYMARKET_OBSERVATIONS)
    polymarket = {
        "asset": "POLYMARKET",
        "stage": "EXPERIMENT",
        "stage_label": "实验中",
        "execution_mode": "READ_ONLY",
        "included_in_portfolio": False,
        "data_state": "NO_OBSERVATIONS",
        "message": "尚无本地观察记录",
        "latest": None,
    }
    if latest:
        edge = _decimal(latest.get("net_edge"))
        stale = _is_stale(latest.get("observed_at"), now)
        polymarket.update({
            "data_state": "STALE_OBSERVATION" if stale else "LOCAL_OBSERVATION",
            "message": "历史纸面记录已过期，不代表当前机会" if stale else "仅显示本地纸面扫描记录，不代表可成交利润",
            "latest": {
                "observed_at": latest.get("observed_at"),
                "question": latest.get("question", ""),
                "net_edge": str(edge) if edge is not None else None,
                "net_roi": latest.get("net_roi"),
                "snapshot_skew_ms": latest.get("snapshot_skew_ms"),
                "positive_after_cost_buffer": bool(edge is not None and edge > 0),
            },
        })
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
            polymarket,
        ],
    }

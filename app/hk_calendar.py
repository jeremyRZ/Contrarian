"""Cached HKEX trading calendar sourced from Futu OpenD."""
from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CALENDAR_FILE = ROOT / ".runtime" / "hk_trading_calendar.json"


def refresh(client, *, start: date | None = None, end: date | None = None) -> dict:
    start = start or (date.today() - timedelta(days=40))
    end = end or (date.today() + timedelta(days=400))
    rows, error = client.trading_days("HK", str(start), str(end))
    if error or rows is None:
        raise RuntimeError(error or "港股交易日历为空")
    payload = {"source": "FUTU_OPEND_HK_TRADE_DATES",
               "updated_at": datetime.now().isoformat(timespec="seconds"),
               "start": str(start), "end": str(end),
               "sessions": {str(row["time"]): str(row.get("trade_date_type") or "WHOLE")
                            for row in rows}}
    CALENDAR_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp = CALENDAR_FILE.with_suffix(".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(CALENDAR_FILE)
    return payload


def _load() -> dict:
    try:
        payload = json.loads(CALENDAR_FILE.read_text(encoding="utf-8"))
        return payload if isinstance(payload.get("sessions"), dict) else {}
    except (OSError, ValueError):
        return {}


def session_type(day: date) -> str | None:
    return _load().get("sessions", {}).get(str(day))


def periods(day: date) -> tuple[tuple[time, time], ...]:
    kind = session_type(day)
    if kind == "WHOLE":
        return ((time(9, 30), time(12)), (time(13), time(16)))
    if kind == "MORNING":
        return ((time(9, 30), time(12)),)
    if kind == "AFTERNOON":
        return ((time(13), time(16)),)
    return ()


def is_session(day: date) -> bool:
    return bool(periods(day))


def next_period_start(now: datetime) -> datetime:
    for offset in range(401):
        day = now.date() + timedelta(days=offset)
        for start, _ in periods(day):
            candidate = datetime.combine(day, start)
            if candidate > now:
                return candidate
    raise RuntimeError("港股交易日历缺失或已过期，调度已停止")


def latest_completed_session(now: datetime, grace_minutes: int = 15) -> date:
    for offset in range(401):
        day = now.date() - timedelta(days=offset)
        day_periods = periods(day)
        if day_periods:
            close = datetime.combine(day, day_periods[-1][1]) + timedelta(minutes=grace_minutes)
            if now >= close:
                return day
    raise RuntimeError("港股交易日历缺失或已过期，无法判定完整日线")

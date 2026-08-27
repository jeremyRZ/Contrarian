import json
from datetime import date, datetime

from app import hk_calendar


def install_calendar(tmp_path, monkeypatch):
    path = tmp_path / "calendar.json"
    path.write_text(json.dumps({"sessions": {
        "2026-08-07": "WHOLE", "2026-08-10": "MORNING",
    }}), encoding="utf-8")
    monkeypatch.setattr(hk_calendar, "CALENDAR_FILE", path)


def test_calendar_handles_holiday_and_half_day(tmp_path, monkeypatch):
    install_calendar(tmp_path, monkeypatch)
    assert hk_calendar.is_session(date(2026, 8, 8)) is False
    assert len(hk_calendar.periods(date(2026, 8, 10))) == 1
    assert hk_calendar.latest_completed_session(datetime(2026, 8, 10, 12, 20)) == date(2026, 8, 10)

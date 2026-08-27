from datetime import datetime
import json

from app import hk_calendar, intraday_scheduler


def _calendar(tmp_path, monkeypatch):
    path = tmp_path / "calendar.json"
    path.write_text(json.dumps({"sessions": {
        "2026-08-07": "WHOLE", "2026-08-10": "WHOLE"}}), encoding="utf-8")
    monkeypatch.setattr(hk_calendar, "CALENDAR_FILE", path)


def test_market_window_excludes_weekends_and_lunch_break(tmp_path, monkeypatch):
    _calendar(tmp_path, monkeypatch)
    assert intraday_scheduler._within_window(datetime(2026, 8, 7, 10, 0), "09:30", "16:00")
    assert not intraday_scheduler._within_window(datetime(2026, 8, 8, 10, 0), "09:30", "16:00")
    assert not intraday_scheduler._within_window(datetime(2026, 8, 7, 12, 30), "09:30", "16:00")
    assert intraday_scheduler._within_window(datetime(2026, 8, 7, 13, 0), "09:30", "16:00")


def test_next_session_after_morning_close_is_afternoon_open(tmp_path, monkeypatch):
    _calendar(tmp_path, monkeypatch)
    now = datetime(2026, 8, 7, 12, 30)
    assert intraday_scheduler._next_session_start(now, "09:30") == datetime(2026, 8, 7, 13, 0)

from datetime import datetime

from app import intraday_scheduler


def test_market_window_excludes_weekends_and_lunch_break():
    assert intraday_scheduler._within_window(datetime(2026, 8, 7, 10, 0), "09:30", "16:00")
    assert not intraday_scheduler._within_window(datetime(2026, 8, 8, 10, 0), "09:30", "16:00")
    assert not intraday_scheduler._within_window(datetime(2026, 8, 7, 12, 30), "09:30", "16:00")
    assert intraday_scheduler._within_window(datetime(2026, 8, 7, 13, 0), "09:30", "16:00")


def test_next_session_after_morning_close_is_afternoon_open():
    now = datetime(2026, 8, 7, 12, 30)
    assert intraday_scheduler._next_session_start(now, "09:30") == datetime(2026, 8, 7, 13, 0)

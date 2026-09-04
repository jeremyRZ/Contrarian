from scripts import watchdog
from datetime import datetime, time


def test_watchdog_records_healthy_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(watchdog, "STATE_FILE", tmp_path / "watchdog.json")
    watchdog.STATE_FILE.write_text('{"last_error":"old"}', encoding="utf-8")
    monkeypatch.setattr(watchdog, "load_config", lambda: {})
    monkeypatch.setattr(watchdog, "_http", lambda *args: {"ok": True})
    monkeypatch.setattr(watchdog, "_port", lambda port: True)
    monkeypatch.setattr(watchdog.hk_calendar, "periods", lambda day: ())
    monkeypatch.setattr(watchdog.notify, "retry_outbox", lambda webhook: {})
    state = watchdog.run(recover=False)
    assert state["website_ok"] is True and state["opend_ok"] is True
    assert "last_error" not in state


def test_daily_catchup_allows_full_refresh_time(tmp_path, monkeypatch):
    class AfterClose(datetime):
        @classmethod
        def now(cls):
            return cls(2026, 9, 4, 17, 0)

    calls = []
    monkeypatch.setattr(watchdog, "datetime", AfterClose)
    monkeypatch.setattr(watchdog, "STATE_FILE", tmp_path / "watchdog.json")
    monkeypatch.setattr(watchdog, "load_config", lambda: {})
    monkeypatch.setattr(watchdog, "_http",
                        lambda url, method="GET", timeout=8: calls.append(
                            (url, method, timeout)) or {"ok": True})
    monkeypatch.setattr(watchdog, "_port", lambda port: True)
    monkeypatch.setattr(watchdog.hk_calendar, "periods",
                        lambda day: ((time(9, 30), time(16, 0)),))
    monkeypatch.setattr(watchdog.notify, "retry_outbox", lambda webhook: {})

    state = watchdog.run(recover=False)
    assert (watchdog.DAILY_URL, "POST", 180) in calls
    assert state["last_daily_catchup"] == "2026-09-04"


def test_external_deadman_heartbeat_is_recorded(tmp_path, monkeypatch):
    monkeypatch.setattr(watchdog, "STATE_FILE", tmp_path / "watchdog.json")
    monkeypatch.setattr(watchdog, "load_config",
                        lambda: {"watchdog": {"heartbeat_url": "https://heartbeat.test/id"}})
    monkeypatch.setattr(watchdog, "_http", lambda *args: {"ok": True})
    monkeypatch.setattr(watchdog, "_port", lambda port: True)
    monkeypatch.setattr(watchdog.hk_calendar, "periods", lambda day: ())
    monkeypatch.setattr(watchdog.notify, "retry_outbox", lambda webhook: {})
    calls = []
    monkeypatch.setattr(watchdog, "_heartbeat", lambda url: calls.append(url))
    state = watchdog.run(recover=False)
    assert calls == ["https://heartbeat.test/id"]
    assert state["external_heartbeat_ok"] is True

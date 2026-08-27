from scripts import watchdog


def test_watchdog_records_healthy_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(watchdog, "STATE_FILE", tmp_path / "watchdog.json")
    monkeypatch.setattr(watchdog, "load_config", lambda: {})
    monkeypatch.setattr(watchdog, "_http", lambda *args: {"ok": True})
    monkeypatch.setattr(watchdog, "_port", lambda port: True)
    monkeypatch.setattr(watchdog.hk_calendar, "periods", lambda day: ())
    monkeypatch.setattr(watchdog.notify, "retry_outbox", lambda webhook: {})
    state = watchdog.run(recover=False)
    assert state["website_ok"] is True and state["opend_ok"] is True

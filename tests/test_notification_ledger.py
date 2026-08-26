from app.modules import notification_ledger


def test_sent_fingerprint_is_persistently_deduplicated(tmp_path, monkeypatch):
    monkeypatch.setattr(notification_ledger, "DB_PATH", tmp_path / "ledger.sqlite3")
    notification_ledger.record("execution:test", "message", "FAILED", channel="WINDOWS_TOAST")
    assert notification_ledger.was_sent_recently("execution:test", 600) is False
    notification_ledger.record("execution:test", "message", "SENT", channel="WINDOWS_TOAST")
    assert notification_ledger.was_sent_recently("execution:test", 600) is True

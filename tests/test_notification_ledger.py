from app.modules import notification_ledger


def test_sent_fingerprint_is_persistently_deduplicated(tmp_path, monkeypatch):
    monkeypatch.setattr(notification_ledger, "DB_PATH", tmp_path / "ledger.sqlite3")
    notification_ledger.record("execution:test", "message", "FAILED", channel="WINDOWS_TOAST")
    assert notification_ledger.was_sent_recently("execution:test", 600) is False
    notification_ledger.record("execution:test", "message", "SENT", channel="WINDOWS_TOAST")
    assert notification_ledger.was_sent_recently("execution:test", 600) is True
    assert notification_ledger.was_sent_recently(
        "execution:test", 600, channel="WECOM") is False


def test_outbox_deduplicates_and_marks_delivery(tmp_path, monkeypatch):
    monkeypatch.setattr(notification_ledger, "DB_PATH", tmp_path / "ledger.sqlite3")
    first = notification_ledger.enqueue("execution:test", "message", "timeout")
    second = notification_ledger.enqueue("execution:test", "message", "timeout")
    assert first == second and len(notification_ledger.due()) == 1
    notification_ledger.mark_delivered(first)
    assert notification_ledger.due() == []

from app import notify
from app.modules import notification_ledger


def test_sent_notification_is_recorded_without_webhook(tmp_path, monkeypatch):
    monkeypatch.setattr(notification_ledger, "DB_PATH", tmp_path / "notifications.sqlite3")
    monkeypatch.setattr(notify, "_send_wecom", lambda text, webhook="", timeout=5: (True, "HTTP_200"))
    assert notify.push_if_new("xiaomi-option:ABC", "BUY CALL", "secret") is True
    row = notification_ledger.dashboard()["records"][0]
    assert row["status"] == "SENT"
    assert row["category"] == "XIAOMI_OPTION"
    assert "secret" not in str(row)


def test_duplicate_attempt_is_recorded(tmp_path, monkeypatch):
    monkeypatch.setattr(notification_ledger, "DB_PATH", tmp_path / "notifications.sqlite3")
    monkeypatch.setattr(notify, "_LAST_PUSH", {"xiaomi-stock:BUY:2026-08-21": 10_000})
    monkeypatch.setattr(notify.time, "time", lambda: 10_001)
    assert notify.push_if_new("xiaomi-stock:BUY:2026-08-21", "BUY STOCK", "secret") is False
    assert notification_ledger.dashboard()["records"][0]["status"] == "SKIPPED_DUPLICATE"

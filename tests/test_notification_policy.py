from app import notify, futu_client
from app.modules import notification_ledger


def test_muted_alerts_and_retries_never_send_but_other_signals_do(tmp_path, monkeypatch):
    monkeypatch.setattr(notification_ledger, "DB_PATH", tmp_path / "ledger.sqlite3")
    monkeypatch.setattr(futu_client, "load_config", lambda: {"notifications": {
        "risk_alerts_enabled": False, "muted_codes": ["HK.08305"]}})
    monkeypatch.setattr(notify, "_LAST_PUSH", {})
    sent = []
    monkeypatch.setattr(notify, "_send_wecom", lambda text, *a: (sent.append(text) is None, "HTTP_200"))
    assert not notify.push_if_new("alert:HK.08305:danger", "圣唐控股(HK.08305)", "test")
    assert not notify.push_wecom("持仓风险 HK.01810", "test")
    assert not notify.push_if_new("daily-div:HK.01810", "daily report", "test")
    notification_ledger.enqueue("alert:HK.08305:danger", "old muted alert")
    notification_ledger.enqueue("production:other", "other signal")
    assert notify.retry_outbox("test") == {"sent": 1, "failed": 0}
    assert notification_ledger.due() == []
    assert sent == ["other signal"]
    assert notification_ledger.dashboard()["summary"]["outbox"]["SUPPRESSED"] == 1
    assert notify.push_if_new("price:HK.01810", "价格报警 HK.01810", "test")
    monkeypatch.setattr(futu_client, "load_config", lambda: {})
    assert notify.push_if_new("alert:HK.08305:new", "持仓风险 HK.08305", "test")


def test_unmuted_failure_still_queues(tmp_path, monkeypatch):
    monkeypatch.setattr(notification_ledger, "DB_PATH", tmp_path / "ledger.sqlite3")
    monkeypatch.setattr(futu_client, "load_config", lambda: {})
    monkeypatch.setattr(notify, "_send_wecom", lambda *a: (False, "timeout"))
    assert not notify.push_if_new("production:failure", "signal", "test")
    assert len(notification_ledger.due()) == 1

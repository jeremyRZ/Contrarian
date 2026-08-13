from app.modules import forward_ledger


def test_record_status_is_append_only_and_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(forward_ledger, "DB_PATH", tmp_path / "ledger.sqlite3")
    status = {"strategies": [{"id": "s1", "name": "策略一", "as_of": "2026-08-13",
                              "action": "WAIT", "reason": "没有机会"}]}
    assert forward_ledger.record_status(status) == 1
    assert forward_ledger.record_status(status) == 0
    result = forward_ledger.dashboard()
    assert result["summary"]["total"] == 1
    assert result["records"][0]["reason"] == "没有机会"


def test_unavailable_signal_is_recorded_for_audit(tmp_path, monkeypatch):
    monkeypatch.setattr(forward_ledger, "DB_PATH", tmp_path / "ledger.sqlite3")
    forward_ledger.record_status({"strategies": [{"id": "s1", "name": "策略一",
        "as_of": None, "action": "UNAVAILABLE", "reason": "行情缺失"}]})
    result = forward_ledger.dashboard()
    assert result["summary"]["unavailable"] == 1

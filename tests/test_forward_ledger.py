from app.modules import forward_ledger
import pandas as pd


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


def test_supertrend_shadow_records_only_a_new_conflict(tmp_path, monkeypatch):
    monkeypatch.setattr(forward_ledger, "DB_PATH", tmp_path / "ledger.sqlite3")
    dates = pd.date_range("2026-01-01", periods=35, freq="B")
    close = pd.Series([10.0] * 14 + list(range(10, 31))[:21])
    bars = pd.DataFrame({"time_key": dates, "open": close, "high": close + .2,
                         "low": close - .2, "close": close})
    direction = pd.Series([1] * 34 + [-1])
    monkeypatch.setattr(forward_ledger, "supertrend", lambda *_args, **_kwargs:
                        pd.DataFrame({"st_direction": direction}))
    assert forward_ledger.record_supertrend_exit_shadow(bars) == 1
    assert forward_ledger.record_supertrend_exit_shadow(bars) == 0
    shadow = forward_ledger.dashboard()["shadow"]
    assert shadow["event_count"] == 1
    assert shadow["events"][0]["details"]["order"] is False

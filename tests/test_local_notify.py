from app import local_notify


def test_windows_toast_uses_encoded_command(monkeypatch):
    captured = {}
    class Result:
        returncode = 0
    monkeypatch.setattr(local_notify.os, "name", "nt")
    monkeypatch.setattr(local_notify.subprocess, "run",
                        lambda args, **kwargs: captured.update({"args": args, "kwargs": kwargs}) or Result())
    ok, detail = local_notify.send("标题", "消息")
    assert ok is True and detail == "WINDOWS_TOAST_OR_BALLOON"
    assert "-EncodedCommand" in captured["args"]
    assert "标题" not in " ".join(captured["args"])

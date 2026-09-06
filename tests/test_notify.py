from app import notify


class _Response:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def test_wecom_http_200_requires_success_errcode(monkeypatch):
    monkeypatch.setattr(
        notify.requests,
        "post",
        lambda *args, **kwargs: _Response(
            payload={"errcode": 93000, "errmsg": "invalid webhook url"}
        ),
    )

    assert notify._send_wecom("test", "https://example.invalid") == (
        False,
        "WECOM_ERR_93000",
    )


def test_wecom_errcode_zero_is_success(monkeypatch):
    monkeypatch.setattr(
        notify.requests,
        "post",
        lambda *args, **kwargs: _Response(payload={"errcode": 0, "errmsg": "ok"}),
    )

    assert notify._send_wecom("test", "https://example.invalid") == (
        True,
        "WECOM_ERR_0",
    )


def test_wecom_http_200_invalid_json_is_failure(monkeypatch):
    monkeypatch.setattr(
        notify.requests,
        "post",
        lambda *args, **kwargs: _Response(payload=ValueError("bad json")),
    )

    assert notify._send_wecom("test", "https://example.invalid") == (
        False,
        "INVALID_JSON",
    )

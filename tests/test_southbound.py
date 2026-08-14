from app.modules import southbound


def _payload():
    return {
        "result": {
            "data": [{
                "SECURITY_NAME": "小米集团-W",
                "HOLD_DATE": "2026-08-13 00:00:00",
                "HOLD_SHARES": 1000,
                "HOLD_SHARES_CHANGE": 100,
                "HOLD_SHARES_RATIO": 1.2,
                "TOTAL_SHARES_RATIO": 0.8,
                "CLOSE_PRICE": 25.5,
                "CHANGE_RATE": 1.0,
            }]
        }
    }


def test_holding_falls_back_to_last_successful_disk_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(southbound, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(southbound, "_http_json", lambda *args, **kwargs: _payload())
    live, err = southbound.holding.__wrapped__("HK.01810")
    assert err is None
    assert live["source"] == "eastmoney-hsgt"

    def blocked(*args, **kwargs):
        raise PermissionError(10013, "socket blocked")

    monkeypatch.setattr(southbound, "_http_json", blocked)
    cached, err = southbound.holding.__wrapped__("HK.01810")

    assert err is None
    assert cached["source"] == "eastmoney-hsgt-cache"
    assert cached["stale"] is True
    assert cached["date"] == "2026-08-13"


def test_holding_hides_socket_details_when_no_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(southbound, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(
        southbound, "_http_json",
        lambda *args, **kwargs: (_ for _ in ()).throw(PermissionError(10013, "socket blocked")),
    )

    data, err = southbound.holding.__wrapped__("HK.01810")

    assert data is None
    assert err == "南向持股数据源暂不可达，且暂无历史缓存"
    assert "10013" not in err

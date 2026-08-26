from __future__ import annotations

import pandas as pd
from fastapi.testclient import TestClient

from app import api


class FakeRouter:
    def daily_bars(self, code, **kwargs):
        dates = pd.bdate_range("2024-01-01", periods=90)
        return pd.DataFrame({"date": dates, "open": range(90), "high": range(1, 91),
                             "low": range(90), "close": range(1, 91),
                             "volume": [1000] * 90, "code": [code] * 90}), None

    def search(self, query, market, limit):
        return [{"code": "SH.603993", "symbol": "603993", "market": "CN",
                 "exchange": "SSE", "currency": "CNY", "asset_type": "STOCK",
                 "name": "洛阳钼业"}], None


def test_markets_endpoint_enables_us_when_tiger_is_configured(monkeypatch):
    monkeypatch.setitem(api.CONFIG, "tiger", {"enabled": True})
    result = api.get_markets()["data"]
    markets = {item["market"]: item for item in result["markets"]}
    assert markets["CN"]["lot_size"] == 100
    assert markets["US"]["enabled"] is True
    assert markets["US"]["positions_enabled"] is True


def test_markets_distinguish_quotes_from_unconfigured_position_accounts(monkeypatch):
    monkeypatch.setitem(api.CONFIG, "futu", {"accounts": {"CN": ""}})
    monkeypatch.setitem(api.CONFIG, "tiger", {"enabled": False})
    markets = {item["market"]: item for item in api.get_markets()["data"]["markets"]}
    assert markets["CN"]["enabled"] is True
    assert markets["CN"]["positions_enabled"] is False
    assert markets["US"]["positions_enabled"] is False


def test_market_bars_endpoint_is_json_serializable(monkeypatch):
    monkeypatch.setattr(api, "market_data", lambda: FakeRouter())
    client = TestClient(api.app)
    response = client.get("/api/securities/SH.603993/bars?count=90")
    assert response.status_code == 200
    assert response.json()["data"]["items"][0]["date"] == "2024-01-01"


def test_cn_search_uses_shared_security_schema(monkeypatch):
    monkeypatch.setattr(api, "market_data", lambda: FakeRouter())
    result = api.search_securities("洛阳", "CN", 20)
    assert result["data"]["items"][0]["code"] == "SH.603993"

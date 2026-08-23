from fastapi.testclient import TestClient

from app import api
from app.modules import research_assets


def test_status_keeps_watch_assets_outside_live_portfolio():
    result = research_assets.status()
    assets = {item["asset"]: item for item in result["assets"]}

    assert assets["BTC"]["stage"] == "WATCH"
    assert assets["BTC"]["included_in_portfolio"] is False
    assert assets["BTC"]["data_state"] == "PROVIDER_NOT_CONFIGURED"
    assert set(assets) == {"BTC"}


def test_research_assets_endpoint_is_read_only(monkeypatch):
    monkeypatch.setattr(research_assets, "status", lambda: {"assets": [{"asset": "BTC"}]})
    response = TestClient(api.app).get("/api/research-assets")

    assert response.status_code == 200
    assert response.json() == {"ok": True, "data": {"assets": [{"asset": "BTC"}]}}

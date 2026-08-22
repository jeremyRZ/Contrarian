import json

from fastapi.testclient import TestClient

from app import api
from app.modules import research_assets


def test_status_keeps_watch_assets_outside_live_portfolio(tmp_path):
    result = research_assets.status(tmp_path / "missing.jsonl")
    assets = {item["asset"]: item for item in result["assets"]}

    assert assets["BTC"]["stage"] == "WATCH"
    assert assets["BTC"]["included_in_portfolio"] is False
    assert assets["BTC"]["data_state"] == "PROVIDER_NOT_CONFIGURED"
    assert assets["POLYMARKET"]["execution_mode"] == "READ_ONLY"
    assert assets["POLYMARKET"]["latest"] is None


def test_status_reads_latest_valid_polymarket_observation(tmp_path):
    path = tmp_path / "observations.jsonl"
    path.write_text(
        "not-json\n" + json.dumps({
            "observed_at": "2026-08-22T01:00:00+00:00",
            "question": "Earlier?", "net_edge": "0.2",
        }) + "\n" + json.dumps({
            "observed_at": "2099-08-22T02:00:00+00:00",
            "question": "Latest?", "net_edge": "-0.1", "net_roi": "-0.01",
            "snapshot_skew_ms": 25,
        }) + "\n",
        encoding="utf-8",
    )

    result = research_assets.status(path)
    item = next(asset for asset in result["assets"] if asset["asset"] == "POLYMARKET")
    assert item["data_state"] == "LOCAL_OBSERVATION"
    assert item["latest"]["question"] == "Latest?"
    assert item["latest"]["positive_after_cost_buffer"] is False


def test_status_marks_old_polymarket_observation_stale(tmp_path):
    path = tmp_path / "observations.jsonl"
    path.write_text(json.dumps({
        "observed_at": "2020-01-01T00:00:00+00:00",
        "question": "Old?", "net_edge": "1",
    }) + "\n", encoding="utf-8")

    item = next(asset for asset in research_assets.status(path)["assets"]
                if asset["asset"] == "POLYMARKET")
    assert item["data_state"] == "STALE_OBSERVATION"
    assert "过期" in item["message"]


def test_research_assets_endpoint_is_read_only(monkeypatch):
    monkeypatch.setattr(research_assets, "status", lambda: {"assets": [{"asset": "BTC"}]})
    response = TestClient(api.app).get("/api/research-assets")

    assert response.status_code == 200
    assert response.json() == {"ok": True, "data": {"assets": [{"asset": "BTC"}]}}

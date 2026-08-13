import pandas as pd

from app.modules import strategy_center


def test_strategy_center_has_no_order_placement_api():
    assert not hasattr(strategy_center, "place_order")
    assert not hasattr(strategy_center, "submit_order")


def test_positions_degrades_to_empty_on_error():
    class Broken:
        def positions(self):
            raise RuntimeError("no trade context")

    assert strategy_center._positions(Broken()) == {}


def test_read_daily_deduplicates_latest_bar(tmp_path):
    p = tmp_path / "bars.csv"
    pd.DataFrame({"time_key": ["2026-08-12", "2026-08-12"], "close": [1, 2]}).to_csv(p, index=False)
    result = strategy_center._read_daily(p)
    assert len(result) == 1
    assert result.iloc[0].close == 2


def test_breakout_strategy_is_exposed_read_only(monkeypatch):
    monkeypatch.setattr(strategy_center, "_positions", lambda client: {})
    monkeypatch.setattr(strategy_center, "_xiaomi_status", lambda p: {"id": "x"})
    monkeypatch.setattr(strategy_center, "_rotation_status", lambda p: {"id": "r"})
    monkeypatch.setattr(strategy_center, "_breakout_status", lambda p: {"id": "b", "action": "WAIT"})
    result = strategy_center.get_status(object(), refresh=False)
    assert [x["id"] for x in result["strategies"]] == ["x", "r", "b"]

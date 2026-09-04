import pandas as pd

from app.modules import forward_ledger


def test_universe_snapshot_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(forward_ledger, "DB_PATH", tmp_path / "ledger.db")
    universe = {"stocks": [{"code": "HK.00700", "price": 500.0}]}
    assert forward_ledger.record_universe_snapshot(universe, "2026-08-22") == 1
    assert forward_ledger.record_universe_snapshot(universe, "2026-08-22") == 0


def test_managed_codes_only_tracks_shadow_buys(tmp_path, monkeypatch):
    monkeypatch.setattr(forward_ledger, "DB_PATH", tmp_path / "ledger.db")
    monkeypatch.setattr(forward_ledger, "DAILY_DIR", tmp_path)
    strategy = {"id": "hk_liquid_trend_rotation_v2", "is_review_day": True,
                "as_of": "2026-08-22", "shadow_targets": [
                    {"code": "HK.00700", "price": 40, "lot_size": 100}]}
    assert forward_ledger.record_rotation_shadow(strategy) == 1
    assert forward_ledger.managed_codes(strategy["id"]) == set()
    pd.DataFrame([{"time_key": "2026-08-24", "open": 40, "close": 40}]).to_csv(
        tmp_path / "HK_00700.csv", index=False)
    assert forward_ledger.settle_paper_orders() == 1
    assert forward_ledger.managed_codes(strategy["id"]) == {"HK.00700"}


def test_xiaomi_shadow_records_one_independent_position(tmp_path, monkeypatch):
    monkeypatch.setattr(forward_ledger, "DB_PATH", tmp_path / "ledger.db")
    monkeypatch.setattr(forward_ledger, "DAILY_DIR", tmp_path)
    pd.DataFrame([
        {"time_key": "2026-08-24", "open": 25.0, "close": 25.5},
        {"time_key": "2026-08-31", "open": 27.0, "close": 27.2},
    ]).to_csv(tmp_path / "HK_01810.csv", index=False)
    buy = {"id": "xiaomi_trend_v1", "action": "BUY", "as_of": "2026-08-22",
           "price": 25.0, "suggested_qty": 200, "reason": "trend"}
    assert forward_ledger.record_xiaomi_shadow(buy) == 1
    assert forward_ledger.record_xiaomi_shadow(buy) == 0
    assert forward_ledger.settle_paper_orders() == 1
    sell = {"id": "xiaomi_trend_v1", "action": "SELL", "as_of": "2026-08-29",
            "price": 27.0, "reason": "exit"}
    assert forward_ledger.record_xiaomi_shadow(sell) == 1
    assert forward_ledger.settle_paper_orders() == 1
    dashboard = forward_ledger._paper_dashboard()
    assert dashboard["order_count"] == 2
    assert dashboard["strategy_metrics"][0]["complete_round_trips"] == 1
    assert dashboard["strategy_metrics"][0]["realized_pnl_hkd"] > 0

    original_fill = dashboard["orders"][1]["fill_price"]
    pd.DataFrame([
        {"time_key": "2026-08-24", "open": 99.0, "close": 99.0},
        {"time_key": "2026-08-31", "open": 88.0, "close": 88.0},
    ]).to_csv(tmp_path / "HK_01810.csv", index=False)
    assert forward_ledger.settle_paper_orders() == 0
    assert forward_ledger._paper_dashboard()["orders"][1]["fill_price"] == original_fill

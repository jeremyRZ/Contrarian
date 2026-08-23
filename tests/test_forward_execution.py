import pandas as pd

from app.modules import forward_ledger


def test_universe_snapshot_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(forward_ledger, "DB_PATH", tmp_path / "ledger.db")
    universe = {"stocks": [{"code": "HK.00700", "price": 500.0}]}
    assert forward_ledger.record_universe_snapshot(universe, "2026-08-22") == 1
    assert forward_ledger.record_universe_snapshot(universe, "2026-08-22") == 0


def test_managed_codes_only_tracks_shadow_buys(tmp_path, monkeypatch):
    monkeypatch.setattr(forward_ledger, "DB_PATH", tmp_path / "ledger.db")
    strategy = {"id": "hk_liquid_trend_rotation_v2", "is_review_day": True,
                "as_of": "2026-08-22", "candidates": [{"code": "HK.00700", "price": 500}],
                "orders": [{"code": "HK.00700", "action": "BUY", "current_qty": 0,
                            "target_qty": 100, "difference_qty": 100}]}
    assert forward_ledger.record_rotation_shadow(strategy) == 1
    assert forward_ledger.managed_codes(strategy["id"]) == {"HK.00700"}

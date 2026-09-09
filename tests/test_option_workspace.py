from datetime import datetime
import json
import pandas as pd
import pytest
from app.modules import option_workspace as w


def quote(**changes):
    return {"code": "HK.TEST", "stock_owner": "HK.00700", "option_type": "CALL",
            "strike_time": "2026-10-29", "option_contract_multiplier": 100,
            "lot_size": 1000, "bid_price": 2, "ask_price": 2.1, "last_price": 2,
            "option_strike_price": 100, "option_delta": .5,
            "volume": 100, "option_open_interest": 200, **changes}


def rank(**changes):
    return w.rank_snapshot(pd.DataFrame([quote(**changes)]), {}, datetime(2026, 9, 9))


def test_multiplier_and_put():
    call = rank()[0]
    assert call["premium"] == 210
    assert call["breakeven_at_expiry"] == 102.1
    put = rank(option_type="PUT", option_delta=-.5)[0]
    assert put["breakeven_at_expiry"] == 97.9
    assert put["liquidity_pass"] and put["within_budget"] is None
    assert not put["actionable"]


@pytest.mark.parametrize("bid,ask,available", [(0, 2, False), (2, 0, False), (3, 2, False), (2, 2, True)])
def test_quote_boundaries(bid, ask, available):
    assert rank(bid_price=bid, ask_price=ask)[0]["quote_available"] is available


def test_expiry_multiplier_and_delta():
    assert not rank(strike_time="2026-09-10")
    assert not rank(option_contract_multiplier=None)
    assert not rank(option_type="PUT", option_delta=.5)[0]["liquidity_pass"]


def test_persistence_tracking_and_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(w, "DB_PATH", tmp_path / "quotes.db")
    monkeypatch.setattr(w, "CACHE_PATH", tmp_path / "quotes.json")
    monkeypatch.setattr(w, "LAST_ERROR", None)
    expiry = (pd.Timestamp.now() + pd.Timedelta(days=30)).strftime("%Y-%m-%d")
    class Client:
        codes = ["HK.TEST"]
        fail = False
        def option_rankings(self, market):
            return {"contracts": pd.DataFrame({"code": self.codes}), "underlyings": pd.DataFrame(),
                    "underlying_total": 154, "contract_total": 80000}, None
        def market_snapshot(self, codes):
            self.requested = codes
            if self.fail:
                return None, "snapshot failed"
            return pd.DataFrame([quote(code=c, strike_time=expiry) for c in codes]), None
    client = Client()
    first = w.refresh(client, {})
    assert first["candidates"][0]["quote_return_pct"] is None
    client.codes = ["HK.NEXT"]
    second = w.refresh(client, {})
    assert "HK.TEST" in client.requested
    tracked = next(r for r in second["candidates"] if r["code"] == "HK.TEST")
    assert tracked["quote_return_pct"] == pytest.approx((2 / 2.1 - 1) * 100)
    client.fail = True
    with pytest.raises(ValueError, match="snapshot failed"):
        w.refresh(client, {})
    assert w.cached()["generated_at"] == second["generated_at"]
    assert w.cached()["scan_error"] == "snapshot failed"
    assert not w.SCAN_LOCK.locked()
    with w.connect() as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert db.execute("SELECT COUNT(*) FROM scans").fetchone()[0] == 2

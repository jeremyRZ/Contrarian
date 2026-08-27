from __future__ import annotations

import pandas as pd
import pytest

from app.markets import cn_lot_size, cn_price_limit, get_market_rules, normalize_code, resolve_security
from app.providers.router import MarketDataRouter
from app.providers.tiger import TigerPositionsProvider
from app import futu_client


def test_code_normalization_is_market_neutral():
    assert normalize_code("603993") == "SH.603993"
    assert normalize_code("000001") == "SZ.000001"
    assert normalize_code("aapl") == "US.AAPL"
    assert resolve_security("SH.603993").currency == "CNY"
    with pytest.raises(ValueError):
        normalize_code("bad code")


def test_cn_rules_apply_lot_t1_and_sell_tax():
    rules = get_market_rules("SH.603993")
    assert rules.market == "CN"
    assert rules.settlement == "T+1"
    assert rules.round_buy_quantity(255) == 200
    assert rules.commission("SELL", 1000, 20) > rules.commission("BUY", 1000, 20)
    assert cn_price_limit("SH.688001") == .20
    assert cn_price_limit("SZ.300001") == .20
    assert cn_lot_size("SH.688001") == 200


def test_router_uses_local_cn_cache_when_futu_is_unavailable(tmp_path, monkeypatch):
    class Client:
        def history_kline(self, *args, **kwargs): return None, "no permission"
    import app.providers.router as module
    monkeypatch.setattr(module, "ROOT", tmp_path)
    runtime = tmp_path / ".runtime"; runtime.mkdir()
    pd.DataFrame([{"date": "2026-01-01", "open": 1, "high": 2, "low": .5,
                   "close": 1.5, "volume": 10}]).to_csv(runtime / "sh603993_qfq_daily.csv", index=False)
    frame, err = MarketDataRouter(Client()).daily_bars("SH.603993")
    assert err is None
    assert frame.iloc[0].code == "SH.603993"


def test_cn_positions_use_isolated_cn_trade_context(monkeypatch):
    captured = {}; closed = []
    class Context:
        def __init__(self, **kwargs): captured.update(kwargs)
        def position_list_query(self, **kwargs):
            captured.update(kwargs); return futu_client.ft.RET_OK, pd.DataFrame([{"code": "SH.603993"}])
        def close(self): closed.append(True)
    monkeypatch.setattr(futu_client, "_reachable", lambda *args: True)
    monkeypatch.setattr(futu_client.ft, "OpenSecTradeContext", Context)
    client = futu_client.FutuClient(accounts={"CN": "123"})
    frame, err = client.positions_market("CN")
    assert err is None and frame.iloc[0].code == "SH.603993"
    assert captured["filter_trdmarket"] == futu_client.ft.TrdMarket.CN
    assert captured["acc_id"] == 123
    assert closed


def test_cn_positions_auto_discovers_matching_account(monkeypatch):
    captured = {}
    class Context:
        def __init__(self, **kwargs): pass
        def get_acc_list(self):
            return futu_client.ft.RET_OK, pd.DataFrame([
                {"acc_id": 11, "trd_env": "SIMULATE"},
                {"acc_id": 22, "trd_env": "REAL"},
            ])
        def position_list_query(self, **kwargs):
            captured.update(kwargs); return futu_client.ft.RET_OK, pd.DataFrame()
        def close(self): pass
    monkeypatch.setattr(futu_client, "_reachable", lambda *args: True)
    monkeypatch.setattr(futu_client.ft, "OpenSecTradeContext", Context)
    _, err = futu_client.FutuClient(trd_env="REAL").positions_market("CN")
    assert err is None
    assert captured["acc_id"] == 22


def test_positions_auto_combines_multiple_matching_accounts(monkeypatch):
    queried = []
    class Context:
        def __init__(self, **kwargs): pass
        def get_acc_list(self):
            return futu_client.ft.RET_OK, pd.DataFrame([
                {"acc_id": 11, "trd_env": "REAL"},
                {"acc_id": 22, "trd_env": "REAL"},
            ])
        def position_list_query(self, **kwargs):
            queried.append(kwargs["acc_id"])
            return futu_client.ft.RET_OK, pd.DataFrame([
                {"code": f"HK.{kwargs['acc_id']:05d}", "qty": 100}
            ])
        def close(self): pass
    monkeypatch.setattr(futu_client, "_reachable", lambda *args: True)
    monkeypatch.setattr(futu_client.ft, "OpenSecTradeContext", Context)
    frame, err = futu_client.FutuClient(trd_env="REAL").positions_market("HK")
    assert err is None
    assert queried == [11, 22]
    assert frame["account_id"].tolist() == [11, 22]


def test_positions_multiple_accounts_return_empty_without_error(monkeypatch):
    class Context:
        def __init__(self, **kwargs): pass
        def get_acc_list(self):
            return futu_client.ft.RET_OK, pd.DataFrame([
                {"acc_id": 11, "trd_env": "REAL"},
                {"acc_id": 22, "trd_env": "REAL"},
            ])
        def position_list_query(self, **kwargs):
            return futu_client.ft.RET_OK, pd.DataFrame()
        def close(self): pass
    monkeypatch.setattr(futu_client, "_reachable", lambda *args: True)
    monkeypatch.setattr(futu_client.ft, "OpenSecTradeContext", Context)
    frame, err = futu_client.FutuClient(trd_env="REAL").positions_market("HK")
    assert err is None and frame.empty


def test_account_summary_combines_all_matching_accounts_without_ids(monkeypatch):
    class Context:
        def __init__(self, **kwargs): pass
        def get_acc_list(self):
            return futu_client.ft.RET_OK, pd.DataFrame([
                {"acc_id": 11, "trd_env": "REAL"},
                {"acc_id": 22, "trd_env": "REAL"},
            ])
        def accinfo_query(self, **kwargs):
            values = {11: (20911.4, 33185.4, 12274.0), 22: (0, 0, 0)}[kwargs["acc_id"]]
            return futu_client.ft.RET_OK, pd.DataFrame([{
                "cash": values[0], "total_assets": values[1], "market_val": values[2]}])
        def close(self): pass
    monkeypatch.setattr(futu_client, "_reachable", lambda *args: True)
    monkeypatch.setattr(futu_client.ft, "OpenSecTradeContext", Context)
    summary, err = futu_client.FutuClient(trd_env="REAL").account_summary_market("HK")
    assert err is None
    assert summary["cash"] == 20911.4 and summary["total_assets"] == 33185.4
    assert summary["matching_accounts"] == 2 and summary["active_accounts"] == 1
    assert "acc_id" not in summary and "account_id" not in summary


def test_account_summary_tolerates_one_failed_account(monkeypatch):
    class Context:
        def __init__(self, **kwargs): pass
        def get_acc_list(self):
            return futu_client.ft.RET_OK, pd.DataFrame([
                {"acc_id": 11, "trd_env": "REAL"}, {"acc_id": 22, "trd_env": "REAL"}])
        def accinfo_query(self, **kwargs):
            if kwargs["acc_id"] == 11:
                return -1, "temporary failure"
            return futu_client.ft.RET_OK, pd.DataFrame([{"cash": 8000, "total_assets": 10000}])
        def close(self): pass
    monkeypatch.setattr(futu_client, "_reachable", lambda *args: True)
    monkeypatch.setattr(futu_client.ft, "OpenSecTradeContext", Context)
    summary, err = futu_client.FutuClient(trd_env="REAL").account_summary_market("HK")
    assert err is None and summary["cash"] == 8000


def test_router_uses_tiger_for_us_positions():
    class Futu:
        def positions_market(self, market):
            raise AssertionError("US positions must not use Futu")
    class Tiger:
        def positions(self):
            return pd.DataFrame([{"code": "US.AAPL", "provider": "tiger"}]), None
    frame, err = MarketDataRouter(Futu(), Tiger()).positions("US")
    assert err is None
    assert frame.iloc[0].code == "US.AAPL"


def test_tiger_position_is_converted_to_shared_schema():
    class Contract:
        symbol = "AAPL"; name = "Apple"; currency = "USD"
    class Position:
        contract = Contract(); position_qty = 2.5; quantity = 2
        salable_qty = 2.5; average_cost = 180; market_price = 200
        market_value = 500; unrealized_pnl = 50; unrealized_pnl_percent = .111
    row = TigerPositionsProvider._position_row(Position())
    assert row["code"] == "US.AAPL"
    assert row["qty"] == 2.5
    assert row["provider"] == "tiger" and row["read_only"] is True


def test_tiger_errors_do_not_expose_sdk_request_details(tmp_path, monkeypatch):
    (tmp_path / "tiger_openapi_config.properties").write_text("placeholder=true", encoding="utf-8")
    import tigeropen.trade.trade_client as trade_module
    monkeypatch.setattr(trade_module.TradeClient, "get_positions",
                        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("account=secret sign=secret")))
    _, err = TigerPositionsProvider(str(tmp_path)).positions()
    assert "account=secret" not in err and "sign=secret" not in err

import inspect
import asyncio

import pandas as pd

from app import api


def test_health_payload_does_not_expose_account_id(monkeypatch):
    class FakeClient:
        def reachable(self): return True

    monkeypatch.setattr(api, "client", lambda: FakeClient())
    monkeypatch.setitem(api.CONFIG, "futu", {"host": "127.0.0.1", "port": 11111, "acc_id": "secret"})
    result = api.health()
    assert "acc_id" not in result["futu"]


def test_get_endpoints_cannot_trigger_push_side_effects():
    assert "push" not in inspect.signature(api.get_daily_divergence).parameters


def test_intraday_scheduler_runs_formal_execution_before_price_risk(monkeypatch):
    captured = []
    monkeypatch.setattr(api.intraday_scheduler, "start", lambda cfg, fns: captured.extend(fns))
    api._start_intraday_scheduler()
    assert captured == [api._formal_execution_run, api._price_alert_run, api._position_risk_run]


def test_formal_execution_run_pushes_time_aware_alert(monkeypatch):
    alert = {"fingerprint": "execution:xiaomi:test", "message": "formal",
             "title": "title", "phase": "PREOPEN"}
    pushed = []
    monkeypatch.setattr(api, "_formal_execution_payload", lambda: alert)
    monkeypatch.setattr(api.notify, "push_if_new",
                        lambda *args, **kwargs: pushed.append((args, kwargs)) or True)
    result = api._formal_execution_run()
    assert result["pushed"] == 1
    assert pushed[0][0][0] == alert["fingerprint"]


def test_xiaomi_directional_get_is_read_only(monkeypatch):
    monkeypatch.setattr(api.xiaomi_directional, "live_status",
                        lambda client, cfg: ({"action": "WAIT"}, None))
    monkeypatch.setattr(api, "client", lambda: object())
    result = api.get_xiaomi_directional()
    assert result == {"ok": True, "data": {"action": "WAIT"}}


def test_xiaomi_options_get_is_read_only(monkeypatch):
    monkeypatch.setattr(api.xiaomi_options, "analyze",
                        lambda client, cfg: ({"instrument": "NONE"}, None))
    monkeypatch.setattr(api, "client", lambda: object())
    result = api.get_xiaomi_options()
    assert result == {"ok": True, "data": {"instrument": "NONE"}}


def test_daily_job_publishes_only_canonical_strategy_notifications(monkeypatch):
    status = {"mode": "READ_ONLY_PAPER_ADVICE",
              "data_freshness": {"status": "CURRENT"}, "strategies": []}
    pushed = []
    monkeypatch.setattr(api.strategy_center, "get_status", lambda *_args, **_kwargs: status)
    monkeypatch.setattr(api.forward_ledger, "record_status", lambda _status: None)
    monkeypatch.setattr(api.forward_ledger, "record_supertrend_exit_shadow", lambda: None)
    monkeypatch.setattr(api.forward_ledger, "settle_paper_orders", lambda: None)
    monkeypatch.setattr(api.daily_report, "run_daily_report", lambda *_args, **_kwargs: {"ok": True})
    monkeypatch.setattr(api, "_funds_note", lambda *_args: "实时资金测试快照")
    monkeypatch.setattr(api.signal_governance, "production_notifications",
                        lambda _status: [("production:test", "formal")])
    monkeypatch.setattr(api.signal_governance, "watchlist_notifications",
                        lambda _status: [("watchlist:test", "daily discoveries")])
    monkeypatch.setattr(api.notify, "push_if_new",
                        lambda fp, text, *_args, **_kwargs: pushed.append((fp, text)))
    monkeypatch.setattr(api.xiaomi_directional, "notification",
                        lambda _status: (_ for _ in ()).throw(AssertionError("research push called")))
    monkeypatch.setattr(api.xiaomi_options, "notification",
                        lambda _status: (_ for _ in ()).throw(AssertionError("option push called")))

    result = api._daily_jobs()

    assert result == {"ok": True}
    assert pushed == [
        ("production:test", "formal\n实时资金测试快照"),
        ("watchlist:test", "daily discoveries\n实时资金测试快照"),
    ]


def test_daily_job_is_the_only_writer_of_strategy_snapshots(monkeypatch):
    status = {"universe": {"stocks": [{"code": "HK.01810"}]}, "portfolio": {},
              "strategies": [
                  {"id": "xiaomi_trend_v1", "as_of": "2026-09-04"},
                  {"id": "hk_liquid_trend_rotation_v2", "as_of": "2026-09-04"},
              ]}
    calls = []
    monkeypatch.setattr(api.strategy_center, "get_status", lambda *_args, **_kwargs: status)
    monkeypatch.setattr(api.notify, "retry_outbox", lambda *_args: None)
    monkeypatch.setattr(api.forward_ledger, "record_universe_snapshot",
                        lambda universe, date: calls.append(("universe", date)))
    monkeypatch.setattr(api.forward_ledger, "record_rotation_shadow",
                        lambda strategy: calls.append(("rotation", strategy["id"])))
    monkeypatch.setattr(api.forward_ledger, "record_xiaomi_shadow",
                        lambda strategy: calls.append(("xiaomi", strategy["id"])))
    monkeypatch.setattr(api.forward_ledger, "settle_paper_orders",
                        lambda: calls.append(("settle", True)))
    monkeypatch.setattr(api.forward_ledger, "record_status",
                        lambda value: calls.append(("status", value is status)))
    monkeypatch.setattr(api.forward_ledger, "record_supertrend_exit_shadow", lambda: None)
    monkeypatch.setattr(api.daily_report, "run_daily_report", lambda *_args, **_kwargs: {"ok": True})
    monkeypatch.setattr(api, "_funds_note", lambda *_args: "test funds")
    monkeypatch.setattr(api.signal_governance, "production_notifications", lambda _status: [])

    assert api._daily_jobs() == {"ok": True}
    assert calls == [("universe", "2026-09-04"),
                     ("rotation", "hk_liquid_trend_rotation_v2"),
                     ("xiaomi", "xiaomi_trend_v1"), ("settle", True),
                     ("status", True)]


def test_position_risk_notification_includes_live_position_and_funds(monkeypatch):
    result = {
        "positions": [{"code": "HK.08305", "qty": 70000, "market_val": 4410}],
        "alerts": [{"code": "HK.08305", "name": "圣唐控股",
                    "level": "danger", "msg": "触及止损线"}],
    }
    captured = {}
    monkeypatch.setattr(api.monitor, "monitor_positions", lambda *_args, **_kwargs: (result, None))
    monkeypatch.setattr(api, "_funds_note", lambda *_args: "实时资金测试快照")
    monkeypatch.setattr(api.notify, "notify_alerts",
                        lambda alerts, *_args, **kwargs: captured.update(
                            alerts=alerts, funds_note=kwargs.get("funds_note")) or 1)

    response = api._position_risk_run()

    assert response["pushed"] == 1
    assert "实时持仓70000股" in captured["alerts"][0]["msg"]
    assert "市值约HK$4,410.00" in captured["alerts"][0]["msg"]
    assert captured["funds_note"] == "实时资金测试快照"


def test_app_lifespan_starts_and_stops_both_schedulers(monkeypatch):
    calls = []
    monkeypatch.setattr(api, "_startup_scheduler", lambda: calls.append("daily-start"))
    monkeypatch.setattr(api, "_start_intraday_scheduler", lambda: calls.append("intraday-start"))
    monkeypatch.setattr(api.scheduler, "stop_scheduler", lambda: calls.append("daily-stop"))
    monkeypatch.setattr(api.intraday_scheduler, "stop", lambda: calls.append("intraday-stop"))
    monkeypatch.setattr(api.hk_calendar, "refresh", lambda client: None)

    async def exercise_lifespan():
        async with api.app.router.lifespan_context(api.app):
            assert calls == ["daily-start", "intraday-start"]

    asyncio.run(exercise_lifespan())
    assert calls == ["daily-start", "intraday-start", "intraday-stop", "daily-stop"]


def test_holdings_include_option_underlying_for_quick_analysis(monkeypatch):
    class FakeClient:
        def positions(self):
            return pd.DataFrame([
                {"code": "HK.MIU260828C34000", "stock_name": "小米 260828 34.00 购", "qty": 2},
                {"code": "HK.08305", "stock_name": "圣唐控股", "qty": 50000},
            ]), None

    monkeypatch.setattr(api, "client", lambda: FakeClient())
    monkeypatch.setattr(api.filters, "is_tradable", lambda client, code: (True, None))

    result = api.get_holdings()
    stocks = result["data"]["stocks"]
    positions = result["data"]["positions"]

    assert {item["code"] for item in stocks} == {"HK.01810", "HK.08305"}
    xiaomi = next(item for item in stocks if item["code"] == "HK.01810")
    assert xiaomi["source"] == "期权正股"
    assert xiaomi["derivatives"] == ["HK.MIU260828C34000"]
    option = next(item for item in positions if item["code"] == "HK.MIU260828C34000")
    assert option["analysis_code"] == "HK.01810"
    assert option["qty"] == 2.0


def test_holdings_do_not_hide_owned_stock_because_valuation_is_missing(monkeypatch):
    class FakeClient:
        def positions(self):
            return pd.DataFrame([
                {"code": "HK.02706", "stock_name": "海致科技集团", "qty": 400},
            ]), None

    monkeypatch.setattr(api, "client", lambda: FakeClient())
    monkeypatch.setattr(api.filters, "is_tradable", lambda client, code: (False, "无估值"))

    result = api.get_holdings()

    assert [item["code"] for item in result["data"]["stocks"]] == ["HK.02706"]


def test_holdings_consolidate_options_and_hide_excluded_delisted_stock(monkeypatch):
    class FakeClient:
        def positions(self):
            return pd.DataFrame([
                {"code": "HK.MIU260828C34000", "stock_name": "小米 260828 34.00 购", "qty": 32},
                {"code": "HK.MIU260828C32000", "stock_name": "小米 260828 32.00 购", "qty": 17},
                {"code": "HK.07709", "stock_name": "南方东英SK海力士每日杠杆最多 (2x) 产品", "qty": 200},
                {"code": "HK.44165", "stock_name": "中国绿宝", "qty": 50000},
            ]), None

    monkeypatch.setattr(api, "client", lambda: FakeClient())
    monkeypatch.setitem(api.CONFIG, "monitor", {"holdings_exclude": ["HK.44165"]})

    stocks = api.get_holdings()["data"]["stocks"]

    assert [item["code"] for item in stocks] == ["HK.01810", "HK.07709"]
    assert stocks[0]["derivatives"] == ["HK.MIU260828C32000", "HK.MIU260828C34000"]


def test_analysis_evidence_blocks_decision_without_financial_and_consensus_data():
    result = api._analysis_evidence(
        analysis={"price": 10, "technical": {"ma20": 9}},
        southbound_data=None,
        buyback_data={"buybacks": []},
        news_data={"news": [{"title": "公司公告"}]},
        capital_flow_data={"flow": {"summary": {"main": 1}}},
        fundamentals_data={"valuation": {"pe": {"current": 10}}},
    )

    assert result["readiness"] == "INSUFFICIENT"
    assert "财务趋势" in result["missing"]
    assert "分析师一致预期" in result["missing"]
    assert "分析师评级" in result["missing"]
    assert "券商研报" not in result["missing"]  # unsupported coverage is disclosed, not mislabeled missing

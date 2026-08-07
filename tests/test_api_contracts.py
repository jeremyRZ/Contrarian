import inspect
import asyncio

from app import api


def test_backtest_run_is_async_so_request_body_can_be_awaited():
    assert inspect.iscoroutinefunction(api.post_backtest_run)


def test_health_payload_does_not_expose_account_id(monkeypatch):
    class FakeClient:
        def connect(self):
            return True, "connected"

    monkeypatch.setattr(api, "client", lambda: FakeClient())
    monkeypatch.setitem(api.CONFIG, "futu", {"host": "127.0.0.1", "port": 11111, "acc_id": "secret"})
    result = api.health()
    assert "acc_id" not in result["futu"]


def test_get_endpoints_cannot_trigger_push_side_effects():
    assert "push" not in inspect.signature(api.get_daily_divergence).parameters
    assert "push" not in inspect.signature(api.get_intraday_scan).parameters


def test_app_lifespan_starts_and_stops_both_schedulers(monkeypatch):
    calls = []
    monkeypatch.setattr(api, "_startup_scheduler", lambda: calls.append("daily-start"))
    monkeypatch.setattr(api, "_start_intraday_scheduler", lambda: calls.append("intraday-start"))
    monkeypatch.setattr(api.scheduler, "stop_scheduler", lambda: calls.append("daily-stop"))
    monkeypatch.setattr(api.intraday_scheduler, "stop", lambda: calls.append("intraday-stop"))

    async def exercise_lifespan():
        async with api.app.router.lifespan_context(api.app):
            assert calls == ["daily-start", "intraday-start"]

    asyncio.run(exercise_lifespan())
    assert calls == ["daily-start", "intraday-start", "intraday-stop", "daily-stop"]

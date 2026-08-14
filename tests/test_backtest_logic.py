import pandas as pd
import pytest

from app.modules import backtest, bt_backtest


def test_stats_reports_worst_trade_excursion_not_average_as_maximum():
    stats = bt_backtest._stats([1.0, -0.5], [2.0, 7.0])
    assert stats["max_adverse_excursion"] == 7.0
    assert stats["max_drawdown"] is None
    assert stats["sharpe"] is None
    assert stats["metric_scope"] == "trade"


def test_default_backtest_uses_next_bar_execution():
    cfg = bt_backtest.strategy_config.DEFAULT_STRATEGY_CONFIG
    assert cfg["backtest"]["exec_on_close"] is False
    assert cfg["backtest"]["forward_days"] == 10


def test_backtest_config_rejects_negative_transaction_costs():
    with pytest.raises(ValueError, match="commission.*不能为负数"):
        bt_backtest.strategy_config._validate({
            "strategies": {"sample": {}},
            "backtest": {"commission": -0.001},
        })


def test_hk_commission_charges_statutory_fees_and_stamp_on_both_sides():
    commission = bt_backtest.HKCommission(
        rate=0.0005,
        stamp=0.001,
        sfc_levy=0.000027,
        afrc_levy=0.0000015,
        trading_fee=0.0000565,
        settlement_fee=0.000042,
    )

    assert commission._getcommission(1000, 10.0, False) == 16.28
    assert commission._getcommission(-1000, 10.0, False) == 16.28


def test_equal_weight_sleeves_produce_a_portfolio_equity_curve():
    dates = pd.to_datetime(["2026-01-02", "2026-01-05"])
    curves = {
        "HK.00001": pd.Series([100000.0, 110000.0], index=dates),
        "HK.00002": pd.Series([100000.0, 90000.0], index=dates),
    }

    equity = bt_backtest._aggregate_equal_weight_curves(curves, initial_capital=100000.0)

    assert equity.tolist() == [100000.0, 100000.0]


def test_portfolio_metrics_use_daily_equity_not_trade_samples():
    dates = pd.to_datetime(["2026-01-02", "2026-01-05", "2026-01-06", "2026-01-07"])
    equity = pd.Series([100000.0, 110000.0, 99000.0, 120000.0], index=dates)

    metrics = bt_backtest._portfolio_metrics(equity, risk_free_rate=0.0)

    assert metrics["metric_scope"] == "portfolio_daily"
    assert metrics["total_return"] == 20.0
    assert metrics["max_drawdown"] == 10.0
    assert metrics["observations"] == 3
    assert metrics["cvar_95"] >= metrics["var_95"]


def test_history_features_use_actual_highs_and_lows_for_52_week_range():
    dates = pd.date_range("2026-01-01", periods=20, freq="D")
    raw = pd.DataFrame({
        "open": [10.0] * 20,
        "high": [20.0] + [11.0] * 19,
        "low": [5.0] + [9.0] * 19,
        "close": [10.0] * 20,
        "volume": [1000.0] * 20,
        "turnover": [0.01] * 20,
    }, index=dates)

    hi52, lo52 = bt_backtest._rolling_52week_range(raw)

    assert hi52.iloc[-1] == 20.0
    assert lo52.iloc[-1] == 5.0


def test_temporal_validation_keeps_holdout_chronologically_after_train():
    dates = pd.date_range("2025-01-01", periods=100, freq="D")
    equity = pd.Series(range(100000, 100100), index=dates, dtype=float)

    result = bt_backtest._temporal_validation(equity)

    assert result["method"] == "chronological_holdout"
    assert result["split_date"] == dates[80].strftime("%Y-%m-%d")
    assert result["holdout"]["observations"] == 20
    assert result["is_pristine_oos"] is False


def test_single_stock_report_does_not_replace_broad_evidence(monkeypatch):
    bt_backtest.clear_caches(include_frames=False)
    reports = iter([
        {"overall": {"n": 20}, "per_strategy": {}},
        {"overall": {"n": 1}, "per_strategy": {}},
    ])
    monkeypatch.setattr(bt_backtest, "run_backtest", lambda *args, **kwargs: next(reports))

    broad = bt_backtest.cached_report(None, {}, object(), 250, 10, "HK.800700")
    bt_backtest.cached_report(["HK.00700"], {}, object(), 250, 10, "HK.800700")

    assert bt_backtest.get_cached_evidence_report() is broad


def test_backtest_compatibility_module_exports_evidence_report():
    assert backtest.get_cached_evidence_report is bt_backtest.get_cached_evidence_report

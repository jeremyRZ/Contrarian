"""
历史回测模块（兼容壳）

真实回测引擎已迁至 bt_backtest.py（backtrader 接管执行/统计/成本建模）。
本模块仅做 re-export，保证 decision.py / api.py 的既有 import 不变：
  - run_backtest(...)
  - cached_report(...)
  - STRATEGY_LABELS
  - _REPORT_CACHE（决策层清回测缓存时用）
"""
from .bt_backtest import (  # noqa: F401
    STRATEGY_LABELS,
    run_backtest,
    cached_report,
    _REPORT_CACHE,
    build_feature_frame,
    latest_trend,
    debug_signals,
    sweep,
    _hstech_crash_map,
)

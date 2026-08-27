"""轻量后台调度器（无第三方依赖）。

在 uvicorn 进程内起一个守护线程，每天指定时间（默认 16:30，HK 收盘后）
运行「每日持仓资金面背离报告」推送。进程常驻即自动按点推送。
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta
from . import hk_calendar

logger = logging.getLogger("hk-scheduler")
_started = False
_thread = None
_stop = threading.Event()


def _next_run_time(hour: int, minute: int) -> float:
    """Calculate the next post-close run from the cached HKEX calendar."""
    now = datetime.now()
    for offset in range(401):
        day = now.date() + timedelta(days=offset)
        periods = hk_calendar.periods(day)
        if not periods:
            continue
        configured = datetime.combine(day, datetime.min.time()).replace(hour=hour, minute=minute)
        close = datetime.combine(day, periods[-1][1])
        target = min(configured, close + timedelta(minutes=30))
        if target > now:
            return target.timestamp()
    raise RuntimeError("港股交易日历缺失或已过期，每日调度已停止")


def start_scheduler(run_fn, hour: int = 16, minute: int = 30, enabled: bool = True) -> None:
    """启动守护线程调度。

    run_fn: 无参可调用（执行每日报告推送）；enabled=False 时不启动。
    重复调用只会启动一次（模块级 _started 保护）。
    """
    global _started, _thread
    if not enabled:
        logger.info("[Scheduler] 已禁用（config.schedule.enabled=false）")
        return
    if _started:
        return
    _started = True
    _stop.clear()

    def loop() -> None:
        logger.info("[Scheduler] 启动，每日 %02d:%02d 推送持仓背离报告", hour, minute)
        while not _stop.is_set():
            t = _next_run_time(hour, minute)
            sleep_s = t - time.time()
            if sleep_s > 0 and _stop.wait(sleep_s):
                break
            try:
                run_fn()
            except Exception as e:  # noqa: BLE001
                logger.error("[Scheduler] 执行每日报告失败: %s", e)
            # 防止同一分钟内被重复触发：多睡 60s
            if _stop.wait(60):
                break

    _thread = threading.Thread(target=loop, name="daily-scheduler", daemon=True)
    _thread.start()


def stop_scheduler(timeout: float = 5.0) -> None:
    """停止每日调度线程；用于应用 lifespan 关闭和测试隔离。"""
    global _started, _thread
    if not _started:
        return
    _stop.set()
    if _thread is not None and _thread.is_alive():
        _thread.join(timeout=max(0.0, timeout))
    if _thread is not None and _thread.is_alive():
        logger.warning("[Scheduler] 停止超时，等待当前任务自行退出")
        return
    _thread = None
    _started = False

"""盘中扫描调度器（交易时段守护线程）。

- 仅在周一至周五 HK 交易时段（默认 09:30–16:00）内、按间隔运行 run_fn
- 支持运行时开关 / 间隔 / 阈值调整，并持久化到 config.yaml（best-effort）
- 暴露 status() 供前端展示当前调度状态与统计

设计要点：
  - 非交易时段睡眠至下一交易时段开盘，避免在休市时反复空跑
  - 交易时段内按 interval_min 睡眠（分块 sleep，可被 stop / 开关切换唤醒）
  - run_fn 由调用方注入（即 intraday.run_intraday），本模块不感知业务
"""
from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timedelta

import yaml

logger = logging.getLogger("hk-intraday-sched")

_started = False
_cfg: dict = {}
_run_fns = []
_stop = threading.Event()

# 运行时状态（前端读取）
_state = {
    "enabled": True,
    "running": False,
    "last_run": None,
    "next_run": None,
    "runs": 0,
    "pushes": 0,
    "last_index_change": None,
    "last_crash": None,
    "strategy_scans": 0,
    "strategy_pushes": 0,
    "last_strategy_time": None,
    "interval_min": 30,
    "window": "09:30-16:00",
    "threshold": -2.0,
    "hstech_code": "HK.800700",
}

DEFAULT_CFG = {
    "enabled": True,
    "interval_min": 30,
    "start": "09:30",
    "end": "16:00",
    "threshold": -2.0,
    "hstech_code": "HK.800700",
}

CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config.yaml")


def _load_cfg(base: dict) -> dict:
    c = dict(DEFAULT_CFG)
    c.update(base or {})
    return c


def _within_window(now: datetime, start: str, end: str) -> bool:
    try:
        sh, sm = (int(x) for x in str(start).split(":"))
        eh, em = (int(x) for x in str(end).split(":"))
    except (ValueError, AttributeError):
        sh, sm, eh, em = 9, 30, 16, 0
    t = now.time()
    lo = timedelta(hours=sh, minutes=sm)
    hi = timedelta(hours=eh, minutes=em)
    cur = timedelta(hours=t.hour, minutes=t.minute, seconds=t.second)
    return lo <= cur <= hi


def _next_open(now: datetime, start: str) -> datetime:
    try:
        sh, sm = (int(x) for x in str(start).split(":"))
    except (ValueError, AttributeError):
        sh, sm = 9, 30
    target = now.replace(hour=sh, minute=sm, second=0, microsecond=0)
    while True:
        if target > now and target.weekday() < 5:  # 0=周一 … 4=周五
            return target
        target += timedelta(days=1)
        target = target.replace(hour=sh, minute=sm, second=0, microsecond=0)


def _chunk_sleep(total: float, step: float = 5.0) -> None:
    """分块 sleep，可被 _stop 唤醒提前退出。"""
    remaining = max(0.0, total)
    while remaining > 0 and not _stop.is_set():
        s = min(step, remaining)
        _stop.wait(s)
        remaining -= s


def _save_config(updates: dict) -> bool:
    """把 intraday 子配置持久化到 config.yaml（best-effort，失败仅记日志）。"""
    try:
        data = {}
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        data.setdefault("intraday", {})
        data["intraday"].update(updates)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
        return True
    except Exception as e:  # noqa: BLE001
        logger.error("[IntradaySched] 持久化 config.yaml 失败: %s", e)
        return False


def start(base_cfg: dict, run_fns) -> None:
    """启动调度守护线程（仅一次）。run_fns: 可调用列表，每个无参、返回 scan 结果 dict。"""
    global _started, _cfg, _run_fns
    if _started:
        return
    _started = True
    _cfg = _load_cfg(base_cfg)
    if callable(run_fns):
        run_fns = [run_fns]
    _run_fns = list(run_fns or [])
    _refresh_state_meta()
    t = threading.Thread(target=_loop, name="intraday-sched", daemon=True)
    t.start()
    logger.info("[IntradaySched] 启动，窗口 %s，间隔 %d 分钟，急跌阈值 %.1f%%，扫描函数 %d 个",
                _state["window"], _cfg.get("interval_min", 30),
                _cfg.get("threshold", -2.0), len(_run_fns))


def _refresh_state_meta() -> None:
    _state["enabled"] = bool(_cfg.get("enabled", True))
    _state["interval_min"] = int(_cfg.get("interval_min", 30))
    _state["threshold"] = float(_cfg.get("threshold", -2.0))
    _state["window"] = f'{_cfg.get("start", "09:30")}-{_cfg.get("end", "16:00")}'
    _state["hstech_code"] = _cfg.get("hstech_code", "HK.800700")


def status() -> dict:
    s = dict(_state)
    s["enabled"] = bool(_cfg.get("enabled", True)) if _cfg else s["enabled"]
    s["interval_min"] = int(_cfg.get("interval_min", 30)) if _cfg else s["interval_min"]
    s["threshold"] = float(_cfg.get("threshold", -2.0)) if _cfg else s["threshold"]
    return s


def set_enabled(on: bool) -> dict:
    """运行时开关；持久化到 config.yaml。"""
    _cfg["enabled"] = bool(on)
    _state["enabled"] = bool(on)
    _save_config({"enabled": bool(on)})
    if bool(on):
        _stop.clear()
    return status()


def set_interval(minutes: int) -> dict:
    """运行时调整扫描间隔（分钟）。"""
    minutes = max(1, int(minutes))
    _cfg["interval_min"] = minutes
    _state["interval_min"] = minutes
    _save_config({"interval_min": minutes})
    return status()


def set_threshold(threshold: float) -> dict:
    """运行时调整急跌阈值（涨跌%）。"""
    threshold = float(threshold)
    _cfg["threshold"] = threshold
    _state["threshold"] = threshold
    _save_config({"threshold": threshold})
    return status()


def _loop() -> None:
    while not _stop.is_set():
        try:
            enabled = bool(_cfg.get("enabled", True))
            interval = max(1, int(_cfg.get("interval_min", 30)))
            start_s = _cfg.get("start", "09:30")
            end_s = _cfg.get("end", "16:00")
            now = datetime.now()

            if not enabled:
                _state["running"] = False
                _state["next_run"] = None
                _stop.wait(15)  # 关闭时低频轮询，等待被打开
                continue

            if _within_window(now, start_s, end_s):
                _state["running"] = True
                _state["next_run"] = (now + timedelta(minutes=interval)).timestamp()
                _state["runs"] += 1
                _state["last_run"] = now.timestamp()
                for fn in _run_fns:
                    try:
                        res = fn()
                        if isinstance(res, dict):
                            pushed = res.get("pushed", 0) or 0
                            _state["pushes"] += pushed
                            if res.get("scan_type") == "six_strategy":
                                _state["strategy_scans"] += 1
                                _state["strategy_pushes"] += pushed
                                _state["last_strategy_time"] = now.timestamp()
                            else:
                                idx = res.get("index") or {}
                                if idx.get("change_rate") is not None:
                                    _state["last_index_change"] = idx["change_rate"]
                                _state["last_crash"] = bool(res.get("crash"))
                    except Exception as e:  # noqa: BLE001
                        logger.error("[IntradaySched] 执行扫描失败: %s", e)
                _chunk_sleep(interval * 60)
            else:
                _state["running"] = False
                nxt = _next_open(now, start_s)
                _state["next_run"] = nxt.timestamp()
                secs = max(0.0, (nxt - now).total_seconds())
                logger.info("[IntradaySched] 非交易时段，睡眠至 %s", nxt)
                _chunk_sleep(min(secs, 3600))  # 每小时重新评估（或开关切换唤醒）
        except Exception as e:  # noqa: BLE001
            logger.error("[IntradaySched] 调度循环异常: %s", e)
            _stop.wait(30)
    _state["running"] = False

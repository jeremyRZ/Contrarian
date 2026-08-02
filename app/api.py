"""
港股投资研究平台 / Contrarian 港股错杀猎手 - FastAPI 后端
- /health        连接健康检查
- /valuation     估值分析
- /screener      买入机会扫描（6策略 + 仓位感知）
- /missed-scan   错杀观察扫描（Contrarian 核心）
- /monitor       持仓监控风控（止损/止盈/技术面 + 企业微信推送）
- /price-alerts  GET 价格报警检查 / POST 增运行时报警
- /analyze       单票实时技术面分析
- /ipo           新股打新分析（POST）
- 前端静态文件托管在 /
"""
from __future__ import annotations

import json
import os
import threading
import time

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .futu_client import build_client_from_config, load_config
from .modules import valuation, screener, monitor, ipo, missed_scan, price_alert, analyze
from . import notify

BASE_DIR = os.path.dirname(os.path.dirname(__file__))
FRONTEND_DIR = os.path.join(BASE_DIR, "frontend")
CONFIG = load_config()

app = FastAPI(title="Contrarian 港股错杀猎手", version="1.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)

_client = None


def client():
    global _client
    if _client is None:
        _client = build_client_from_config(CONFIG)
    return _client


def _wrap(result, error):
    if error:
        return JSONResponse(status_code=200, content={"ok": False, "error": error})
    return {"ok": True, "data": result}


def _webhook() -> str:
    return CONFIG.get("wecom", {}).get("webhook", "")


@app.get("/health")
def health():
    ok, msg = client().connect()
    return {"ok": ok, "connected": ok, "message": msg,
            "system": CONFIG.get("system", {}),
            "futu": CONFIG.get("futu", {})}


@app.get("/valuation")
def get_valuation(code: str, financials: str = ""):
    fin = {}
    if financials:
        try:
            fin = json.loads(financials)
        except Exception:  # noqa: BLE001
            fin = {}
    result, err = valuation.value_stock(client(), code, financials=fin or None)
    return _wrap(result, err)


@app.get("/screener")
def get_screener(top_n: int = 20, codes: str = ""):
    code_list = [c.strip() for c in codes.split(",") if c.strip()] if codes else None
    sc = CONFIG.get("screener", {})
    result, err = screener.screen(
        client(), codes=code_list, top_n=top_n,
        hstech_code=sc.get("hstech_code", "HK.800000"),
        cash_full_stop=sc.get("cash_full_stop", True),
    )
    return _wrap(result, err)


@app.get("/missed-scan")
def get_missed_scan(top_n: int = 5, pool: str = ""):
    ms = CONFIG.get("missed_scan", {})
    pool = pool or ms.get("pool", "leaders")
    result, err = missed_scan.missed_scan(
        client(), pool=pool, top_n=top_n or ms.get("top_n", 5),
        min_drop_pct=ms.get("min_drop_pct", 20.0),
        hstech_code=CONFIG.get("screener", {}).get("hstech_code", "HK.800000"),
    )
    if result and _webhook():
        notify.notify_missed(result, _webhook())
    return _wrap(result, err)


@app.get("/monitor")
def get_monitor():
    tech = CONFIG.get("monitor", {}).get("technical", True)
    result, err = monitor.monitor_positions(client(), technical=tech)
    if result and result.get("alerts"):
        notify.notify_alerts(result["alerts"], _webhook(),
                             prefix=f"{CONFIG.get('system', {}).get('notify_prefix', '')} 持仓预警")
    return _wrap(result, err)


@app.get("/price-alerts")
def get_price_alerts():
    result, err = price_alert.evaluate_all(client(), CONFIG)
    if result and result.get("alerts_to_push") and _webhook():
        notify.notify_alerts(
            [{"code": a["code"], "name": a["name"], "level": a["level"], "msg": a["msg"]}
             for a in result["alerts_to_push"]],
            _webhook(),
            prefix=f"{CONFIG.get('system', {}).get('notify_prefix', '')} 价格报警",
        )
    return _wrap(result, err)


@app.post("/price-alerts")
async def post_price_alert(request: Request):
    try:
        data = await request.json()
    except Exception:  # noqa: BLE001
        data = {}
    if not data.get("code"):
        return JSONResponse(status_code=200, content={"ok": False, "error": "缺少 code"})
    item = price_alert.add_runtime(data)
    return {"ok": True, "data": item}


@app.get("/analyze")
def get_analyze(code: str):
    result, err = analyze.analyze(client(), code)
    return _wrap(result, err)


@app.post("/ipo")
async def post_ipo(request: Request):
    try:
        data = await request.json()
    except Exception:  # noqa: BLE001
        data = {}
    result, err = ipo.analyze_ipo(data or {})
    return _wrap(result, err)


@app.get("/ipo/meta")
def ipo_meta():
    return {
        "ok": True,
        "data": {
            "weights": ipo.WEIGHTS,
            "dim_labels": ipo.DIM_LABELS,
            "rarity_tiers": ["global_unique", "top3", "front", "red_ocean"],
            "valuation_levels": ["discount", "slight_low", "fair", "slight_high", "high"],
            "calibrated_cases": ipo.CALIBRATED_CASES,
        },
    }


@app.get("/ipo/auto")
def ipo_auto(code: str):
    """联网获取招股信息后自动打分；失败降级为手动输入提示。"""
    result, err = ipo.auto_analyze(code)
    return _wrap(result, err)


# ---------- 后台调度器（轻量线程，无需 APScheduler 依赖） ----------
_sched_stop = threading.Event()


def _scheduler_loop():
    cfg = CONFIG.get("scheduler", {})
    if not cfg.get("enabled", False):
        return
    title = CONFIG.get("system", {}).get("scheduler_title", "Contrarian 交易系统")
    while not _sched_stop.is_set():
        try:
            cli = client()
            ok, _ = cli.connect()
            if ok:
                # 持仓监控
                res, _ = monitor.monitor_positions(cli, technical=True)
                if res and res.get("alerts"):
                    notify.notify_alerts(res["alerts"], _webhook(), prefix=f"{title} 持仓预警")
                # 错杀扫描
                mres, _ = missed_scan.missed_scan(cli, pool=CONFIG.get("missed_scan", {}).get("pool", "leaders"))
                if mres:
                    notify.notify_missed(mres, _webhook())
                # 价格报警
                pres, _ = price_alert.evaluate_all(cli, CONFIG)
                if pres and pres.get("alerts_to_push"):
                    notify.notify_alerts(
                        [{"code": a["code"], "name": a["name"], "level": a["level"], "msg": a["msg"]}
                         for a in pres["alerts_to_push"]],
                        _webhook(), prefix=f"{title} 价格报警")
        except Exception:  # noqa: BLE001
            pass
        # 以扫描间隔为基准睡眠
        _sched_stop.wait(cfg.get("scan_interval_min", 60) * 60)


@app.on_event("startup")
def _start_scheduler():
    if CONFIG.get("scheduler", {}).get("enabled", False):
        t = threading.Thread(target=_scheduler_loop, daemon=True)
        t.start()


# 托管前端（/ 必须在 API 路由之后）
if os.path.isdir(FRONTEND_DIR):
    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")

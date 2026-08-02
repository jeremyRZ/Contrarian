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
from .modules import valuation, screener, monitor, ipo, missed_scan, price_alert, analyze, buybacks, news, southbound, reverse_signals, capital_flow, southbound_risk, fundamentals, filters, dividend, earnings
from . import notify

BASE_DIR = os.path.dirname(os.path.dirname(__file__))
FRONTEND_DIR = os.path.join(BASE_DIR, "frontend")
CONFIG = load_config()

app = FastAPI(title="Contrarian 港股错杀猎手", version="1.6.1")
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


@app.get("/holdings")
def get_holdings():
    """持仓中的正股（排除窝轮/杠杆ETF），供单票页快速选择。轻量，不做技术面。"""
    try:
        pos, err = client().positions()
    except Exception as e:  # noqa: BLE001
        return _wrap(None, str(e))
    if err:
        return _wrap(None, err)
    if pos is None or pos.empty:
        return _wrap({"stocks": []}, None)
    exclude = set(str(x) for x in (CONFIG.get("monitor", {}).get("holdings_exclude") or []))
    cols = {c.lower(): c for c in pos.columns}
    c_code = cols.get("code")
    c_name = cols.get("stock_name") or cols.get("name")
    out = []
    for _, row in pos.iterrows():
        code = str(row[c_code])
        if code in exclude:
            continue
        name = str(row[c_name]) if c_name else code
        ptype = monitor._classify(name, code)
        if not ptype.startswith("正股"):
            continue
        # 自动剔除停牌 / 无报价 / 无估值的标的
        try:
            ok, _ = filters.is_tradable(client(), code)
        except Exception:  # noqa: BLE001
            ok = True
        if not ok:
            continue
        out.append({"code": code, "name": name, "type": ptype})
    return _wrap({"stocks": out}, None)


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
    if result:
        rev, _ = reverse_signals.reverse_score(client(), code, days=60, num=10)
        if rev:
            result["reverse"] = rev
    return _wrap(result, err)


@app.get("/buybacks")
def get_buybacks(code: str, num: int = 10):
    result, err = buybacks.get_buybacks(client(), code, num=num)
    return _wrap(result, err)


@app.get("/news")
def get_news(code: str, num: int = 10):
    result, err = news.get_news(client(), code, num=num)
    return _wrap(result, err)


@app.get("/southbound")
def get_southbound(code: str = "", days: int = 30):
    # 单票查询只返回「个股港股通持股」，不返回全市场净买额
    # （市场级数据放在个股页会误导用户，以为与个股相关）
    if code:
        holding_res, holding_err = southbound.holding(code)
        if holding_err:
            return _wrap({"holding_error": holding_err}, None)
        return _wrap({"holding": holding_res}, None)
    # 不带 code 时返回全市场港股通每日净买额（市场概览用途）
    market_res, market_err = southbound.market_netflow(days=days)
    return _wrap({"market": market_res}, market_err)


@app.get("/capital-flow")
def get_capital_flow(code: str, days: int = 20):
    """个股资金流向（大单/超大单）：分布快照 + 每日净流入序列。"""
    dist, derr = capital_flow.distribution(client(), code)
    ser, serr = capital_flow.series(client(), code, days=days)
    return _wrap({
        "distribution": dist,
        "distribution_error": derr,
        "flow": ser,
        "flow_error": serr,
    }, None)


@app.get("/southbound-risk")
def get_southbound_risk(codes: str = ""):
    """聚合个股南向(港股通)减持风险预警（持仓或龙头池）。"""
    code_list = [c.strip() for c in codes.split(",") if c.strip()] if codes else None
    result, err = southbound_risk.aggregate(client(), codes=code_list)
    return _wrap(result, err)


@app.get("/fundamentals")
def get_fundamentals(code: str):
    """个股基本面反向信号源数据：估值分位（PE/PB 历史分位 + 行业排名）+ 机构增减持。"""
    val, verr = fundamentals.valuation_signal(client(), code)
    inst, ierr = fundamentals.institution_signal(client(), code)
    return _wrap({
        "valuation": val,
        "valuation_error": verr,
        "institution": inst,
        "institution_error": ierr,
    }, None)


@app.get("/dividend")
def get_dividend(code: str):
    """个股股息率 / 分红反向信号（第 7 档源数据）：TTM 股息率 + 增派/弃派判定。"""
    sig, err = dividend.dividend_signal(client(), code)
    return _wrap(sig, err)


@app.get("/earnings")
def get_earnings(code: str):
    """个股财报窗口信号（第 8 档源数据）：财报季月份启发式 + 可选东财业绩日历。"""
    sig, err = earnings.earnings_signal(code)
    return _wrap(sig, err)


WATCHLIST_FILE = os.path.join(BASE_DIR, "watchlist.json")


def _read_watchlist():
    try:
        if os.path.exists(WATCHLIST_FILE):
            with open(WATCHLIST_FILE, encoding="utf-8") as f:
                return json.load(f)
    except Exception:  # noqa: BLE001
        pass
    return []


def _write_watchlist(items):
    with open(WATCHLIST_FILE, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)


@app.get("/watchlist")
def get_watchlist():
    """读取用户观察池（跨浏览器持久化，存 watchlist.json）。"""
    return {"ok": True, "data": _read_watchlist()}


@app.post("/watchlist")
async def post_watchlist(request: Request):
    """加入 / 移除观察池。body: {code, name?, action?: 'add'|'remove'}。"""
    try:
        data = await request.json()
    except Exception:  # noqa: BLE001
        data = {}
    code = str(data.get("code", "")).strip()
    if not code:
        return JSONResponse(status_code=200, content={"ok": False, "error": "缺少 code"})
    items = _read_watchlist()
    if str(data.get("action")) == "remove":
        items = [x for x in items if x.get("code") != code]
    else:
        if not any(x.get("code") == code for x in items):
            items.append({"code": code, "name": str(data.get("name", "")).strip() or code})
    _write_watchlist(items)
    return {"ok": True, "data": items}


# ---------- 富途自选股（只读） ----------
@app.get("/futu-watchlist")
def get_futu_watchlist():
    """读取富途自选股（「全部」分组，只读）。富途 API 不允许通过接口修改系统分组。"""
    wl, err = client().get_watchlist()
    if err:
        return _wrap(None, err)
    # 额外返回分组信息
    groups, gerr = client().get_watchlist_groups()
    return _wrap({
        "stocks": [{"code": c, "name": n} for c, n in (wl or [])],
        "groups": groups or [],
        "readonly": True,
        "note": "富途自选股为只读（API 不支持修改系统分组），增删请使用本地观察池或在富途客户端操作。",
    }, None)


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

"""
港股投资研究平台 / Contrarian 港股错杀猎手 - FastAPI 后端
- /health        连接健康检查
- /valuation     估值分析
- /screener      买入机会扫描（6策略 + 仓位感知）
- /strategies/push 手动触发 6 策略扫描并推送到企业微信
- /missed-scan   错杀观察扫描（Contrarian 核心）
- /monitor       持仓监控风控（止损/止盈/技术面 + 企业微信推送）
- /price-alerts  GET 价格报警检查 / POST 增运行时报警
- /analyze       单票实时技术面分析
- /ipo           新股打新分析（POST）
- /intraday/scan 盘中「恒科急跌联动」低吸扫描（手动触发 + 可选推送）
- /intraday/status 盘中调度器运行状态与配置
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
from .modules import valuation, screener, monitor, ipo, missed_scan, price_alert, analyze, buybacks, news, southbound, reverse_signals, capital_flow, southbound_risk, fundamentals, filters, dividend, earnings, divergence, daily_report, intraday, backtest, strategy_config, decision
from .modules.screener import LEADERS
from . import scheduler, intraday_scheduler, notify

BASE_DIR = os.path.dirname(os.path.dirname(__file__))
FRONTEND_DIR = os.path.join(BASE_DIR, "frontend")
CONFIG = load_config()

app = FastAPI(title="Contrarian 港股错杀猎手", version="1.8.1")
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


@app.on_event("startup")
def _startup_scheduler():
    """启动每日持仓资金面背离报告调度（默认 16:30 HK 收盘后推送）。"""
    sch = CONFIG.get("schedule", {})
    enabled = bool(sch.get("enabled", True))
    hh, mm = "16:30".split(":")
    try:
        hh, mm = str(sch.get("time", "16:30") or "16:30").split(":")
        hh, mm = int(hh), int(mm)
    except (ValueError, AttributeError):
        hh, mm = 16, 30
    scheduler.start_scheduler(
        lambda: daily_report.run_daily_report(client(), _webhook()),
        hour=hh, minute=mm, enabled=enabled,
    )


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


@app.post("/strategies/push")
def post_strategies_push(force: bool = True):
    """手动触发 6 大策略扫描并立即推送达标信号到企业微信（force 忽略冷却，适合手动按钮）。"""
    cl, meta = _intraday_candidate_codes(client())
    res = screener.run_scheduled_scan(
        client(), codes=cl, webhook=_webhook(),
        hstech_code=CONFIG.get("intraday", {}).get("hstech_code", "HK.800700"),
        cash_full_stop=True, cooldown_sec=14400, max_push=5, force=force,
    )
    return _wrap(res, None)


@app.get("/strategies/scan")
def get_strategies_scan(top_n: int = 50):
    """6 大策略扫描（持仓 + 观察池 + 龙头池组合），供 strategies.html 手动刷新，与推送同池。"""
    cl, meta = _intraday_candidate_codes(client())
    sc = CONFIG.get("screener", {})
    result, err = screener.screen(
        client(), codes=cl, top_n=top_n,
        hstech_code=sc.get("hstech_code", "HK.800700"),
        cash_full_stop=sc.get("cash_full_stop", True),
    )
    return _wrap(result, err)


# ---------- 回测验证（阶段1） ----------
@app.get("/backtest/report")
def get_backtest_report(codes: str = "", forward_days: int = 20,
                        window_days: int = 250, hstech_code: str = "HK.800700",
                        refresh: int = 0, no_fee: int = 0):
    """6 大策略信号历史回测。codes 逗号分隔；为空用龙头池(LEADERS)。refresh=1 强制重算。
    no_fee=1 时不计交易成本（对比视角）。"""
    code_list = [c.strip() for c in codes.split(",") if c.strip()] or None
    cfg = strategy_config.load_config()
    if refresh:
        backtest._REPORT_CACHE.clear()
    rep = backtest.cached_report(
        code_list, cfg, build_client_from_config(CONFIG),
        window_days, forward_days, hstech_code, no_fee=bool(no_fee),
    )
    return _wrap(rep, None)


@app.post("/backtest/run")
def post_backtest_run(request: Request):
    """异步触发回测重算（清空缓存后重跑）。body 可选 {codes, forward_days, window_days}。"""
    try:
        data = json.loads(request.body().decode() or "{}")
    except Exception:  # noqa: BLE001
        data = {}
    codes = data.get("codes") or None
    if isinstance(codes, str):
        codes = [c.strip() for c in codes.split(",") if c.strip()] or None
    forward_days = int(data.get("forward_days", 20))
    window_days = int(data.get("window_days", 250))
    hstech_code = data.get("hstech_code", "HK.800700")
    no_fee = bool(int(data.get("no_fee", 0)))
    backtest._REPORT_CACHE.clear()
    cfg = strategy_config.load_config()
    rep = backtest.run_backtest(
        codes, cfg, build_client_from_config(CONFIG),
        window_days, forward_days, hstech_code, no_fee=no_fee,
    )
    return _wrap(rep, None)


@app.get("/backtest/debug")
def get_backtest_debug(code: str = "HK.00700", window_days: int = 250,
                       hstech_code: str = "HK.800700"):
    """信号诊断：逐根 bar 统计各信号触发频次与评分分布，定位无成交原因。"""
    cfg = strategy_config.load_config()
    rep = backtest.debug_signals(code, cfg, build_client_from_config(CONFIG),
                                 window_days, hstech_code)
    return _wrap(rep, None)


@app.get("/backtest/sweep")
def get_backtest_sweep(codes: str = "", window_days: int = 250,
                       hstech_code: str = "HK.800700",
                       forward: str = "5,10,20", stop: str = "0.04,0.08",
                       rsi2: str = "5,10", focus: str = "RSI2 逆向低吸"):
    """参数寻优：持有天数 × 止损 × RSI2 阈值 网格回测，按期望值排序。"""
    code_list = [c.strip() for c in codes.split(",") if c.strip()] or None
    cfg = strategy_config.load_config()
    fl = [int(x) for x in forward.split(",") if x.strip()]
    sl = [float(x) for x in stop.split(",") if x.strip()]
    rl = [float(x) for x in rsi2.split(",") if x.strip()]
    rep = backtest.sweep(code_list, cfg, build_client_from_config(CONFIG),
                         window_days, hstech_code, fl, sl, rl, focus)
    return _wrap(rep, None)


@app.get("/_debug/kline")
def debug_kline(code: str = "HK.00700", max_count: int = 5):
    """保留占位（已废弃）：回测特征重建见 app/modules/backtest.py。"""
    return _wrap(None, "debug endpoint deprecated")


@app.get("/decision")
def get_decision(code: str):
    """LLM 决策层（M3）：回测门控 → 差异化判断。"""
    res = decision.decide(code, client(), CONFIG)
    return _wrap(res, None)


@app.get("/strategies/config")
def get_strategies_config():
    """返回当前策略参数（strategies.yaml 与默认值合并）。"""
    return _wrap(strategy_config.load_config(), None)


@app.post("/strategies/config")
async def post_strategies_config(request: Request):
    """保存策略参数（M4）：校验后落盘 strategies.yaml，并清空回测缓存以生效。

    body 为部分配置（会与默认值深合并），例如：
    {"strategies": {"deep_drop": {"drop_pct": 30}}, "push": {"light": 6.5}}
    """
    try:
        body = json.loads((await request.body()) or b"{}")
    except Exception as e:  # noqa: BLE001
        return _wrap(None, "配置解析失败：" + str(e))
    if not isinstance(body, dict):
        return _wrap(None, "配置必须是 JSON 对象")
    try:
        saved = strategy_config.save_config(body)
    except ValueError as e:
        return _wrap(None, "配置校验失败：" + str(e))
    # 参数变更后清空回测缓存，下次回测/决策用新参数
    backtest._REPORT_CACHE.clear()
    return _wrap(saved, None)


@app.post("/strategies/config/reset")
def post_strategies_config_reset():
    """恢复策略参数默认并落盘，清空回测缓存。"""
    saved = strategy_config.reset_config()
    backtest._REPORT_CACHE.clear()
    return _wrap(saved, None)


@app.get("/missed-scan")
def get_missed_scan(top_n: int = 5, pool: str = ""):
    ms = CONFIG.get("missed_scan", {})
    pool = pool or ms.get("pool", "leaders")
    result, err = missed_scan.missed_scan(
        client(), pool=pool, top_n=top_n or ms.get("top_n", 5),
        min_drop_pct=ms.get("min_drop_pct", 20.0),
        hstech_code=CONFIG.get("screener", {}).get("hstech_code", "HK.800700"),
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


@app.get("/daily-divergence")
def get_daily_divergence(push: bool = True):
    """每日持仓资金面背离报告。push=true 且已配置 wecom.webhook 时推送企业微信。"""
    webhook = _webhook() if push else ""
    rep = daily_report.run_daily_report(client(), webhook)
    return _wrap(rep, rep.get("error"))


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


@app.get("/_debug/signal")
def debug_signal(code: str = "HK.01810"):
    """临时诊断：单票特征帧 + 6策略信号 + 趋势/关键点位（进程内，复用回测逻辑）。"""
    import math
    import numpy as np
    import pandas as pd

    def _f(v):
        return None if (v is None or (isinstance(v, float) and math.isnan(v))) else float(v)

    cl = client()
    frame, err = backtest.build_feature_frame(cl, code, 450, None)
    if err or frame is None or frame.empty:
        return _wrap(None, f"K线错误: {err}")
    last = frame.iloc[-1]
    prev = frame.iloc[-2]
    closes = frame["close"].astype(float)
    sma50 = _f(last["sma50"]); sma200 = _f(last["sma200"]); rsi2 = _f(last["rsi2"])
    hi52 = _f(last["hi52"]); lo52 = _f(last["lo52"]); pos = _f(last["pos_pct"])
    pe_k = _f(last["pe"])

    def slope(series, n=5):
        s = series.dropna().iloc[-n:]
        if len(s) < 2 or s.iloc[0] == 0:
            return None
        return (s.iloc[-1] - s.iloc[0]) / s.iloc[0] * 100

    sma50_slope = slope(frame["sma50"])
    sma200_slope = slope(frame["sma200"])

    def rsi(series, n=14):
        d = series.diff()
        g = d.clip(lower=0).rolling(n).mean()
        l = (-d.clip(upper=0)).rolling(n).mean()
        rs = g / l
        return float((100 - 100 / (1 + rs)).iloc[-1])

    rsi14 = rsi(closes)

    snap, serr = cl.market_snapshot([code])
    price = prev_close = turnover = amp = pe_snap = None
    up = "n/a"
    if snap is not None and not snap.empty:
        r = snap.iloc[0]
        price = _f(r.get("last_price"))
        prev_close = _f(r.get("prev_close_price"))
        turnover = _f(r.get("turnover_rate"))
        amp = _f(r.get("amplitude"))
        pe_snap = _f(r.get("pe_ratio")) if "pe_ratio" in r.index else None
        up = str(r.get("update_time", ""))
    if price is None:
        price = _f(last["close"])
    if prev_close is None:
        prev_close = _f(prev["close"])
    chg = (price - prev_close) / prev_close * 100 if prev_close else 0.0
    turnover = turnover if turnover is not None else (_f(last["turnover"]) or 0.0) * 100.0
    amp = amp if amp is not None else ((_f(last["high"]) - _f(last["low"])) / prev_close * 100 if prev_close else None)
    pe = pe_snap if pe_snap is not None else pe_k

    hs_snap, _ = cl.market_snapshot(["HK.800700"])
    hs_chg = None
    if hs_snap is not None and not hs_snap.empty:
        hr = hs_snap.iloc[0]
        hpc = _f(hr.get("prev_close_price")); hlp = _f(hr.get("last_price"))
        if hpc:
            hs_chg = (hlp - hpc) / hpc * 100
    cfg = strategy_config.load_config()
    hs_th = cfg["strategies"]["hstech_link"]["hstech_drop"]
    hstech_crash = (hs_chg is not None and hs_chg <= hs_th)

    is_leader = code in LEADERS
    f = {
        "price": price, "change_rate": chg, "prev_close_price": prev_close,
        "turnover_rate": turnover, "amplitude": amp, "pe": pe,
        "hi52": hi52, "lo52": lo52, "pos_pct": pos,
        "is_leader": is_leader, "hstech_crash": hstech_crash,
        "sma50": sma50, "sma200": sma200, "rsi2": rsi2,
    }
    ev = screener.evaluate_signals(f, cfg)
    uptrend = (price is not None and sma200 is not None and price > sma200)
    vol5 = float(frame["volume"].astype(float).tail(5).mean())
    vol_today = _f(last["volume"]) or 0.0
    recent10 = [round(float(c), 2) for c in closes.tail(10).tolist()]
    support = min(lo52, float(closes.tail(20).min()))
    resist = max(float(closes.tail(20).max()), sma50)
    return _wrap({
        "code": code, "is_leader": is_leader, "update_time": up,
        "price": price, "prev_close": prev_close, "change_rate": round(chg, 2),
        "turnover_rate": round(turnover, 2), "amplitude": round(amp, 2) if amp else None,
        "pe": pe,
        "ma50": sma50, "ma50_slope": round(sma50_slope, 2) if sma50_slope is not None else None,
        "ma200": sma200, "ma200_slope": round(sma200_slope, 2) if sma200_slope is not None else None,
        "price_vs_ma50": round((price - sma50) / sma50 * 100, 2) if sma50 else None,
        "price_vs_ma200": round((price - sma200) / sma200 * 100, 2) if sma200 else None,
        "rsi14": round(rsi14, 1), "rsi2": rsi2,
        "week52_high": hi52, "week52_low": lo52, "week52_position_pct": pos,
        "uptrend": uptrend, "hstech_change": round(hs_chg, 2) if hs_chg is not None else None,
        "hstech_crash": hstech_crash,
        "score": ev["score"], "signals": ev["signals"],
        "signal_details": ev.get("signal_details"),
        "reason": screener._build_reason(ev.get("reason_inputs", {}), ev["signals"]),
        "vol_ratio_today_vs5d": round(vol_today / vol5, 2) if vol5 else None,
        "recent10_close": recent10,
        "support": round(support, 2), "resist": round(resist, 2),
        "today_low": _f(last["low"]), "today_high": _f(last["high"]),
    }, None)


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


# ---------- 富途自选股（可增删） ----------
@app.get("/futu-watchlist")
def get_futu_watchlist(group: str = ""):
    """读取富途自选股（用户自建分组，支持增删）。group 可覆盖目标分组。

    注意：get_user_security_group() 在当前 FutuOpenD 环境下会阻塞，故此处不拉取分组列表，
    仅管理单一配置分组（futu.watchlist_group）；系统分组（如「全部」）不可写，写入会返回错误。
    """
    target = group or client().watchlist_group
    wl, err = client().get_watchlist(group=target)
    if err:
        return _wrap(None, err)
    return _wrap({
        "stocks": [{"code": c, "name": n} for c, n in (wl or [])],
        "group": target,
        "readonly": False,
        "note": "已管理用户分组「%s」，可用 POST /futu-watchlist 增删。" % target,
    }, None)


@app.post("/futu-watchlist")
async def post_futu_watchlist(request: Request):
    """增删富途自选股。body: {code, name?, action?: 'add'|'remove', group?}。"""
    try:
        data = await request.json()
    except Exception:  # noqa: BLE001
        data = {}
    code = str(data.get("code", "")).strip()
    if not code:
        return JSONResponse(status_code=200, content={"ok": False, "error": "缺少 code"})
    action = str(data.get("action", "add")).lower()
    group = str(data.get("group", "")).strip() or None
    ok, err = client().modify_watchlist(code, action=action, group=group)
    if not ok:
        gname = group or client().watchlist_group
        return _wrap(None, "富途自选修改失败：" + (err or "未知错误") +
                     "（若提示分组不存在，请先在富途客户端创建用户分组「%s」）" % gname)
    return _wrap({"code": code, "action": action, "group": group or client().watchlist_group}, None)


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


# ---------- 盘中急跌联动 ----------
@app.get("/intraday/scan")
def get_intraday_scan(push: bool = False, threshold: float = None, codes: str = ""):
    """手动触发恒科急跌联动低吸扫描。push=true 且急跌触发时已配置 webhook 则推送企业微信。"""
    cfg = dict(CONFIG.get("intraday", {}) or {})
    if threshold is not None:
        try:
            cfg["threshold"] = float(threshold)
        except (ValueError, TypeError):
            pass
    if codes:
        cl = [c.strip() for c in codes.split(",") if c.strip()]
        meta = {c: {"name": c, "source": "自定义"} for c in cl}
    else:
        cl, meta = _intraday_candidate_codes(client())
    webhook = _webhook() if push else ""
    rep = intraday.run_intraday(client(), webhook, cfg, codes=cl, code_meta=meta)
    return _wrap(rep, rep.get("error"))


@app.get("/intraday/status")
def get_intraday_status():
    """盘中调度器运行状态与配置（不触发扫描）。"""
    return {"ok": True, "data": intraday_scheduler.status()}


@app.post("/intraday/config")
async def post_intraday_config(request: Request):
    """运行时调度配置：{action:'enable'|'interval'|'threshold', value}。持久化到 config.yaml。"""
    try:
        data = await request.json()
    except Exception:  # noqa: BLE001
        data = {}
    action = str(data.get("action", ""))
    try:
        if action == "enable":
            st = intraday_scheduler.set_enabled(bool(data.get("value")))
        elif action == "interval":
            st = intraday_scheduler.set_interval(int(data.get("value")))
        elif action == "threshold":
            st = intraday_scheduler.set_threshold(float(data.get("value")))
        else:
            st = intraday_scheduler.status()
    except (ValueError, TypeError):
        st = intraday_scheduler.status()
    return {"ok": True, "data": st}


def _intraday_candidate_codes(cli):
    """汇总候选池：持仓正股 + 本地观察池 + 龙头池，去重。返回 (codes, code_meta)。"""
    codes: list = []
    meta: dict = {}
    try:
        pos, _ = cli.positions()
        if pos is not None and not pos.empty:
            cols = {c.lower(): c for c in pos.columns}
            c_code = cols.get("code")
            c_name = cols.get("stock_name") or cols.get("name")
            for _, row in pos.iterrows():
                code = str(row[c_code])
                name = str(row[c_name]) if c_name else code
                if code in meta:
                    continue
                meta[code] = {"name": name, "source": "持仓"}
                codes.append(code)
    except Exception:  # noqa: BLE001
        pass
    for it in _read_watchlist():
        code = str(it.get("code", "")).strip()
        if not code or code in meta:
            continue
        meta[code] = {"name": str(it.get("name", "")).strip() or code, "source": "观察"}
        codes.append(code)
    for code in LEADERS:
        if code in meta:
            continue
        meta[code] = {"name": code, "source": "龙头"}
        codes.append(code)
    return codes, meta


def _intraday_run():
    """注入调度器的扫描函数：汇总候选池并跑一次盘中扫描（推送由 run_intraday 内部判定）。"""
    cfg = CONFIG.get("intraday", {}) or {}
    cl, meta = _intraday_candidate_codes(client())
    return intraday.run_intraday(client(), _webhook(), cfg, codes=cl, code_meta=meta)


def _strategy_run():
    """注入调度器的 6 策略扫描函数：汇总候选池并跑一次全策略扫描（推送由 run_scheduled_scan 内部判定）。"""
    cfg = CONFIG.get("intraday", {}) or {}
    cl, meta = _intraday_candidate_codes(client())
    return screener.run_scheduled_scan(
        client(), codes=cl, webhook=_webhook(),
        hstech_code=cfg.get("hstech_code", "HK.800700"),
        cash_full_stop=True, cooldown_sec=14400, max_push=5, force=False,
    )


def _price_alert_run():
    """注入调度器的价格报警函数：交易时段每轮检查 config.price_alert.list，到价则推企业微信。"""
    res, err = price_alert.evaluate_all(client(), CONFIG)
    if res and res.get("alerts_to_push"):
        pushed = notify.notify_alerts(
            [{"code": a["code"], "name": a["name"], "level": a["level"], "msg": a["msg"]}
             for a in res["alerts_to_push"]],
            _webhook(),
            prefix=f"{CONFIG.get('system', {}).get('notify_prefix', '')} 价格报警",
        )
        return {"pushed": pushed, "scan_type": "price_alert"}
    return {"pushed": 0, "scan_type": "price_alert"}


@app.on_event("startup")
def _start_intraday_scheduler():
    """启动盘中调度（交易时段守护线程，默认 09:30–16:00 / 30 分钟）：急跌联动 + 6 大策略扫描 + 价格报警并发。"""
    intraday_scheduler.start(CONFIG.get("intraday", {}) or {}, [_intraday_run, _strategy_run, _price_alert_run])


# 托管前端（/ 必须在 API 路由之后）
if os.path.isdir(FRONTEND_DIR):
    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")

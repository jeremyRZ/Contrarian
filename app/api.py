"""
港股投资决策助手 / Contrarian - FastAPI 后端
- /health        连接健康检查
- /valuation     估值分析
- /strategy-center/status 已通过研究门槛的策略动作
- /monitor       持仓监控风控（止损/止盈/技术面 + 企业微信推送）
- /price-alerts  GET 价格报警检查 / POST 增运行时报警
- /analyze       单票实时技术面分析
- 前端静态文件托管在 /
"""
from __future__ import annotations

import json
import os
import threading
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .futu_client import build_client_from_config, load_config
from .modules import valuation, screener, monitor, ipo, missed_scan, price_alert, analyze, buybacks, news, southbound, reverse_signals, capital_flow, southbound_risk, fundamentals, filters, dividend, earnings, divergence, daily_report, intraday, backtest, strategy_config, decision, strategy_center, forward_ledger, westock_research
from .modules.screener import LEADERS
from . import scheduler, intraday_scheduler, notify

BASE_DIR = os.path.dirname(os.path.dirname(__file__))
FRONTEND_DIR = os.path.join(BASE_DIR, "frontend")
CONFIG = load_config()


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    """统一管理后台调度线程与 Futu 连接。"""
    _startup_scheduler()
    _start_intraday_scheduler()
    try:
        yield
    finally:
        intraday_scheduler.stop()
        scheduler.stop_scheduler()
        global _client
        if _client is not None:
            _client.close()
            _client = None


app = FastAPI(title="Contrarian 港股错杀猎手", version="1.8.1", lifespan=_lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)


@app.get("/strategies.html", include_in_schema=False)
@app.get("/intraday.html", include_in_schema=False)
@app.get("/backtest.html", include_in_schema=False)
def legacy_strategy_page():
    return RedirectResponse("/strategy-center.html", status_code=308)


@app.get("/ipo.html", include_in_schema=False)
def legacy_ipo_page():
    return RedirectResponse("/analyze.html", status_code=308)

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
    def daily_jobs():
        status = strategy_center.get_status(client(), refresh=True)
        forward_ledger.record_status(status)
        return daily_report.run_daily_report(client(), _webhook())
    scheduler.start_scheduler(daily_jobs, hour=hh, minute=mm, enabled=enabled)


@app.get("/health")
def health():
    ok, msg = client().connect()
    futu_cfg = CONFIG.get("futu", {}) or {}
    return {"ok": ok, "connected": ok, "message": msg,
            "system": CONFIG.get("system", {}),
            "futu": {k: futu_cfg.get(k) for k in ("host", "port", "trd_env", "watchlist_group")
                     if k in futu_cfg}}


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
        hstech_code=sc.get("hstech_code", "HK.800700"),
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
def get_backtest_report(codes: str = "", forward_days: int = 10,
                        window_days: int = 250, hstech_code: str = "HK.800700",
                        refresh: int = 0, no_fee: int = 0):
    """6 大策略信号历史回测。codes 逗号分隔；为空用龙头池(LEADERS)。refresh=1 强制重算。
    no_fee=1 时不计交易成本（对比视角）。"""
    code_list = [c.strip() for c in codes.split(",") if c.strip()] or None
    cfg = strategy_config.load_config()
    if refresh:
        backtest.clear_caches()
    rep = backtest.cached_report(
        code_list, cfg, build_client_from_config(CONFIG),
        window_days, forward_days, hstech_code, no_fee=bool(no_fee),
    )
    return _wrap(rep, None)


@app.post("/backtest/run")
async def post_backtest_run(request: Request):
    """异步触发回测重算（清空缓存后重跑）。body 可选 {codes, forward_days, window_days}。"""
    try:
        data = await request.json()
    except Exception:  # noqa: BLE001
        data = {}
    codes = data.get("codes") or None
    if isinstance(codes, str):
        codes = [c.strip() for c in codes.split(",") if c.strip()] or None
    forward_days = int(data.get("forward_days", 10))
    window_days = int(data.get("window_days", 250))
    hstech_code = data.get("hstech_code", "HK.800700")
    no_fee = bool(int(data.get("no_fee", 0)))
    backtest.clear_caches()
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


@app.get("/decision")
def get_decision(code: str):
    """LLM 决策层（M3）：回测门控 → 差异化判断。"""
    res = decision.decide(code, client(), CONFIG)
    return _wrap(res, None)


@app.get("/strategies/config")
def get_strategies_config():
    """返回当前策略参数（strategies.yaml 与默认值合并）。"""
    return _wrap(strategy_config.load_config(), None)


@app.get("/strategy-center/status")
def get_strategy_center_status(refresh: int = 0):
    """Qualified-strategy dashboard. Read-only; never places an order."""
    try:
        status = strategy_center.get_status(client(), refresh=bool(refresh))
        forward_ledger.record_status(status)
        return _wrap(status, None)
    except Exception as e:  # noqa: BLE001
        return _wrap(None, f"策略中心更新失败: {e}")


@app.get("/forward-ledger")
def get_forward_ledger(limit: int = 200):
    return _wrap(forward_ledger.dashboard(max(1, min(limit, 1000))), None)


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
    backtest.clear_caches()
    return _wrap(saved, None)


@app.post("/strategies/config/reset")
def post_strategies_config_reset():
    """恢复策略参数默认并落盘，清空回测缓存。"""
    saved = strategy_config.reset_config()
    backtest.clear_caches()
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
    return _wrap(result, err)


@app.get("/monitor")
def get_monitor():
    tech = CONFIG.get("monitor", {}).get("technical", True)
    result, err = monitor.monitor_positions(client(), technical=tech)
    return _wrap(result, err)


@app.get("/daily-divergence")
def get_daily_divergence():
    """只读生成每日持仓资金面背离报告。"""
    rep = daily_report.run_daily_report(client(), "")
    return _wrap(rep, rep.get("error"))


@app.post("/daily-divergence/push")
def post_daily_divergence_push():
    """显式生成并推送每日持仓资金面背离报告。"""
    rep = daily_report.run_daily_report(client(), _webhook())
    return _wrap(rep, rep.get("error"))


@app.get("/holdings")
def get_holdings():
    """实际正股持仓及衍生品对应正股，供单票页快捷分析。"""
    try:
        pos, err = client().positions()
    except Exception as e:  # noqa: BLE001
        return _wrap(None, str(e))
    if err:
        return _wrap(None, err)
    if pos is None or pos.empty:
        return _wrap({"stocks": [], "positions": []}, None)
    exclude = set(str(x) for x in (CONFIG.get("monitor", {}).get("holdings_exclude") or []))
    cols = {c.lower(): c for c in pos.columns}
    c_code = cols.get("code")
    c_name = cols.get("stock_name") or cols.get("name")
    stocks = {}
    positions = []
    for _, row in pos.iterrows():
        code = str(row[c_code])
        name = str(row[c_name]) if c_name else code
        ptype = monitor._classify(name, code)
        underlying = monitor.derivative_underlying(code, name)
        analysis_code = underlying["code"] if underlying else (
            code if ptype.startswith("正股") or ptype == "杠杆ETF" else None
        )
        positions.append({
            "code": code,
            "name": name,
            "type": ptype,
            "qty": float(row.get(cols.get("qty"), 0) or 0),
            "market_val": float(row.get(cols.get("market_val"), 0) or 0),
            "pl_val": float(row.get(cols.get("pl_val"), 0) or 0),
            "analysis_code": analysis_code,
            "analysis_name": underlying["name"] if underlying else name,
        })
        if ptype.startswith("正股"):
            target = {"code": code, "name": name, "type": ptype,
                      "source": "正股持仓", "derivatives": []}
        elif ptype == "杠杆ETF":
            target = {"code": code, "name": name, "type": ptype,
                      "source": "杠杆ETF持仓", "derivatives": []}
        else:
            if not underlying:
                continue
            target = {**underlying, "type": "正股(衍生品标的)",
                      "source": "期权正股", "derivatives": [code]}
        target_code = target["code"]
        if target_code in exclude:
            continue
        if target_code in stocks:
            stocks[target_code]["derivatives"] = sorted(set(
                stocks[target_code].get("derivatives", []) + target.get("derivatives", [])
            ))
            if target["source"] == "正股持仓":
                derivatives = stocks[target_code]["derivatives"]
                stocks[target_code].update(target)
                stocks[target_code]["derivatives"] = derivatives
        else:
            stocks[target_code] = target
    return _wrap({"stocks": list(stocks.values()), "positions": positions}, None)


@app.get("/price-alerts")
def get_price_alerts():
    result, err = price_alert.evaluate_all(client(), CONFIG, mark_fired=False)
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


def _embedded(result, error=None):
    """生成与独立 API 相同的响应形状，供单票聚合接口复用前端渲染器。"""
    return {"ok": not bool(error), "data": result, "error": error}


def _analysis_evidence(*, analysis, southbound_data, buyback_data, news_data,
                       capital_flow_data, fundamentals_data, research_data=None) -> dict:
    """评估单股结论的数据覆盖度；缺关键基本面时禁止包装成完整决策。"""
    checks = {
        "价格与技术面": bool(analysis.get("price") is not None and
                         (analysis.get("technical") or {}).get("ma20") is not None),
        "资金流向": bool(capital_flow_data and
                     (capital_flow_data.get("flow") or capital_flow_data.get("distribution"))),
        "公司相关新闻": bool(news_data and news_data.get("news")),
        "南向持股": bool((southbound_data and southbound_data.get("holding")) or
                     (research_data or {}).get("south", {}).get("status") == "available"),
        "公司回购": bool(buyback_data is not None),
        "估值数据": bool(fundamentals_data and fundamentals_data.get("valuation")),
        # 下列三项尚无已接通的可靠生产数据源，必须明确呈现为缺失。
        "财务趋势": (research_data or {}).get("finance", {}).get("status") == "available",
        "分析师评级": (research_data or {}).get("rating", {}).get("status") == "available",
        "分析师一致预期": (research_data or {}).get("consensus", {}).get("status") == "available",
    }
    available = [name for name, ok in checks.items() if ok]
    missing = [name for name, ok in checks.items() if not ok]
    critical = {"财务趋势", "分析师评级"}
    readiness = "INSUFFICIENT" if critical & set(missing) else "READY"
    return {
        "available": available,
        "missing": missing,
        "coverage_pct": round(len(available) / len(checks) * 100),
        "readiness": readiness,
        "message": ("证据不足：当前结果只能用于行情观察，不能支持完整买卖决策"
                    if readiness == "INSUFFICIENT" else "关键证据已齐备"),
    }


@app.get("/analyze/full")
def get_analyze_full(code: str):
    """单票聚合分析：一次数据采集返回技术面、反向信号、源数据和决策。"""
    cl = client()
    analysis_result, analysis_err = analyze.analyze(cl, code)
    if not analysis_result:
        return _wrap(None, analysis_err or "分析失败")

    reverse_result, reverse_err = reverse_signals.reverse_score(
        cl, code, days=60, num=10, include_sources=True,
    )
    reverse_result = reverse_result or {"score": 0.0, "signals": [], "details": {}}
    sources = reverse_result.pop("sources", {})
    analysis_result["reverse"] = reverse_result

    sb_data, sb_err = sources.get("southbound", (None, reverse_err))
    bb_data, bb_err = sources.get("buybacks", (None, reverse_err))
    nw_data, nw_err = sources.get("news", (None, reverse_err))
    cf_data, cf_err = sources.get("capital_flow", (None, reverse_err))
    fund_data = sources.get("fundamentals") or {}
    research_data = westock_research.get_research(code)

    decision_result = decision.decide(
        code, cl, CONFIG,
        analysis_result=analysis_result,
        reverse_result=reverse_result,
    )
    evidence = _analysis_evidence(
        analysis=analysis_result,
        southbound_data=sb_data,
        buyback_data=bb_data,
        news_data=nw_data,
        capital_flow_data=cf_data,
        fundamentals_data=fund_data,
        research_data=research_data,
    )
    if evidence.get("readiness") != "READY":
        decision_result = dict(decision_result or {})
        decision_result.update({
            "gated": True,
            "evidence_gated": True,
            "verdict": None,
            "reason": evidence.get("message") or "关键研究证据不足，禁止形成完整买入结论",
            "position_suggestion": "不新增仓位；补齐财务趋势和分析师评级后重新评估",
        })
    return _wrap({
        "analysis": analysis_result,
        "evidence": evidence,
        "extras": {
            "southbound": _embedded(sb_data, sb_err),
            "buybacks": _embedded(bb_data, bb_err),
            "news": _embedded(nw_data, nw_err),
            "capital_flow": _embedded(cf_data, cf_err),
            "fundamentals": _embedded(fund_data),
            "research": _embedded(research_data),
            "decision": _embedded(decision_result),
        },
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
def _run_intraday_scan(push: bool, threshold: float = None, codes: str = ""):
    """运行盘中扫描；由只读查询和显式推送端点共同复用。"""
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


@app.get("/intraday/scan")
def get_intraday_scan(threshold: float = None, codes: str = ""):
    """只读触发恒科急跌联动低吸扫描。"""
    return _run_intraday_scan(False, threshold, codes)


@app.post("/intraday/scan/push")
def post_intraday_scan_push(threshold: float = None, codes: str = ""):
    """显式扫描并在触发时推送企业微信。"""
    return _run_intraday_scan(True, threshold, codes)


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


def _start_intraday_scheduler():
    """交易时段只执行价格风控；未经准入的买入策略不得自动运行。"""
    intraday_scheduler.start(CONFIG.get("intraday", {}) or {}, [_price_alert_run])


# 托管前端（/ 必须在 API 路由之后）
if os.path.isdir(FRONTEND_DIR):
    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")

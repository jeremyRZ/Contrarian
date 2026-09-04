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

import pandas as pd

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .futu_client import build_client_from_config, load_config
from .modules import valuation, monitor, ipo, price_alert, analyze, buybacks, news, southbound, reverse_signals, capital_flow, southbound_risk, fundamentals, filters, dividend, earnings, divergence, daily_report, strategy_center, forward_ledger, notification_ledger, signal_governance, execution_alerts, westock_research, cn_research, research_assets, xiaomi_directional, xiaomi_options, option_mapper
from .markets import cn_lot_size, cn_price_limit, get_market_rules, resolve_security
from .providers import MarketDataRouter, TigerPositionsProvider
from . import hk_calendar, scheduler, intraday_scheduler, notify

BASE_DIR = os.path.dirname(os.path.dirname(__file__))
FRONTEND_DIR = os.path.join(BASE_DIR, "frontend")
CONFIG = load_config()


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    """统一管理后台调度线程与 Futu 连接。"""
    try:
        hk_calendar.refresh(client())
    except Exception as exc:  # noqa: BLE001
        print(f"港股交易日历更新失败，调度将保持关闭: {exc}")
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


app = FastAPI(title="Contrarian 多市场投研平台", version="2.0.0", lifespan=_lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)


@app.get("/ipo.html", include_in_schema=False)
def legacy_ipo_page():
    return RedirectResponse("/analyze.html", status_code=308)

_client = None


def client():
    global _client
    if _client is None:
        _client = build_client_from_config(CONFIG)
    return _client


def market_data():
    tiger_cfg = CONFIG.get("tiger", {}) or {}
    tiger = TigerPositionsProvider(tiger_cfg.get("props_path", ""),
                                   enabled=tiger_cfg.get("enabled", False))
    return MarketDataRouter(client(), tiger_provider=tiger)


def _wrap(result, error):
    if error:
        return JSONResponse(status_code=200, content={"ok": False, "error": error})
    return {"ok": True, "data": result}


def _webhook() -> str:
    return (os.environ.get("CONTRARIAN_WECOM_WEBHOOK", "")
            or CONFIG.get("wecom", {}).get("webhook", ""))


def _funds_note(portfolio: dict | None = None) -> str:
    """Sanitized live HK capital context for every trade-related message."""
    funds = portfolio or {}
    if not funds:
        try:
            funds, _ = client().account_summary_market("HK")
        except Exception:  # noqa: BLE001
            funds = None
    if (not funds or funds.get("funds_complete") is not True
            or funds.get("available_cash") is None or funds.get("total_assets") is None):
        return "富途实时资金不可用；不得据此执行新交易。"
    return (
        f"富途实时资金：可用港币购买力HK${float(funds['available_cash']):,.2f}／"
        f"总资产HK${float(funds['total_assets']):,.2f}；"
        f"快照{funds.get('funds_as_of') or funds.get('as_of') or '刚刚'}。")


def _daily_jobs():
    """Record research, but publish trade language from one canonical source only."""
    notify.retry_outbox(_webhook())
    status = strategy_center.get_status(client(), refresh=True)
    signal_date = next((item.get("as_of") for item in status.get("strategies", [])
                        if item.get("as_of")), None)
    forward_ledger.record_universe_snapshot(status.get("universe", {}), signal_date)
    rotation = next((item for item in status.get("strategies", [])
                     if item.get("id") == "hk_liquid_trend_rotation_v2"), None)
    if rotation:
        forward_ledger.record_rotation_shadow(rotation)
    xiaomi = next((item for item in status.get("strategies", [])
                   if item.get("id") == "xiaomi_trend_v1"), None)
    if xiaomi:
        forward_ledger.record_xiaomi_shadow(xiaomi)
    forward_ledger.settle_paper_orders()
    forward_ledger.record_status(status)
    forward_ledger.record_supertrend_exit_shadow()
    report = daily_report.run_daily_report(client(), _webhook(),
                                           funds_note=_funds_note(status.get("portfolio")))
    for fingerprint, message in signal_governance.production_notifications(status):
        notify.push_if_new(fingerprint, message + "\n" + _funds_note(status.get("portfolio")),
                           _webhook(), min_interval=86_400)
    for fingerprint, message in signal_governance.watchlist_notifications(status):
        notify.push_if_new(fingerprint, message + "\n" + _funds_note(status.get("portfolio")),
                           _webhook(), min_interval=86_400)
    # Xiaomi momentum and option selectors remain readable research endpoints.
    # They intentionally do not run through the trade-notification path.
    return report


@app.post("/internal/daily-jobs")
def post_internal_daily_jobs(request: Request):
    """Local watchdog catch-up; the launcher binds this service to loopback only."""
    host = request.client.host if request.client else ""
    if host not in {"127.0.0.1", "::1", "testclient"}:
        return JSONResponse(status_code=403, content={"ok": False, "error": "LOCAL_ONLY"})
    return _wrap(_daily_jobs(), None)


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
    scheduler.start_scheduler(_daily_jobs, hour=hh, minute=mm, enabled=enabled)


@app.get("/health")
def health():
    futu = client()
    ok = futu.reachable()
    msg = "FutuOpenD 端口可达" if ok else "FutuOpenD 未启动或端口不可达"
    futu_cfg = CONFIG.get("futu", {}) or {}
    return {"ok": ok, "connected": ok, "message": msg,
            "system": CONFIG.get("system", {}),
            "futu": {k: futu_cfg.get(k) for k in ("host", "port", "trd_env", "watchlist_group")
                     if k in futu_cfg}}


@app.get("/api/xiaomi-directional")
def get_xiaomi_directional():
    """Read-only Xiaomi research observation; never a formal trade signal."""
    result, error = xiaomi_directional.live_status(client(), CONFIG)
    return _wrap(result, error)


@app.get("/api/xiaomi-options")
def get_xiaomi_options():
    """Read-only CALL/PUT recommendation; this endpoint never pushes or trades."""
    result, error = xiaomi_options.analyze(client(), CONFIG)
    return _wrap(result, error)


@app.get("/api/option-map")
def get_option_map(code: str, action: str = "WAIT"):
    """Read-only generic option map; never submits or pushes an order."""
    portfolio = strategy_center._portfolio_payload(client())
    result, error = option_mapper.analyze(client(), code, action.upper(), portfolio)
    return _wrap(result, error)


@app.get("/api/notification-ledger")
def get_notification_ledger(limit: int = 200):
    """Read-only audit trail of sent, failed, and deduplicated notifications."""
    return _wrap(notification_ledger.dashboard(max(1, min(limit, 1000))), None)


@app.get("/api/execution-alert")
def get_execution_alert():
    """Current formal execution reminder; read-only and never pushes or orders."""
    return _wrap(_formal_execution_payload(), None)


@app.get("/api/markets")
def get_markets():
    """Stable market metadata used by the shared HK/CN/US frontend."""
    markets = []
    futu_accounts = (CONFIG.get("futu", {}) or {}).get("accounts", {}) or {}
    tiger_enabled = bool((CONFIG.get("tiger", {}) or {}).get("enabled", False))
    for key, name, enabled in (("HK", "港股", True), ("CN", "A股", True),
                               ("US", "美股", tiger_enabled)):
        rules = get_market_rules(key)
        markets.append({"market": key, "name": name, "enabled": enabled,
                        "positions_enabled": (key == "HK" or tiger_enabled) if key != "CN"
                        else bool(futu_accounts.get("CN")),
                        "currency": rules.currency, "benchmark": rules.benchmark,
                        "lot_size": rules.lot_size, "settlement": rules.settlement,
                        "timezone": rules.timezone,
                        "sessions": [[a.strftime("%H:%M"), b.strftime("%H:%M")]
                                     for a, b in rules.sessions]})
    return _wrap({"markets": markets, "default": "HK"}, None)


@app.get("/api/securities/search")
def search_securities(q: str = "", market: str = "CN", limit: int = 20):
    rows, err = market_data().search(q, market, limit)
    return _wrap({"market": market.upper(), "items": rows}, err)


@app.get("/api/securities/{code}/snapshot")
def get_security_snapshot(code: str):
    try: security = resolve_security(code)
    except ValueError as exc: return _wrap(None, str(exc))
    frame, err = market_data().snapshot([security.code])
    if err or frame is None or frame.empty: return _wrap(None, err or "行情为空")
    clean = frame.iloc[0].replace([float("inf"), float("-inf")], None)
    row = clean.where(clean.notna(), None).to_dict()
    return _wrap({"security": security.to_dict(), "snapshot": row}, None)


@app.get("/api/securities/{code}/bars")
def get_security_bars(code: str, start: str = "", end: str = "", count: int = 260):
    try: security = resolve_security(code)
    except ValueError as exc: return _wrap(None, str(exc))
    frame, err = market_data().daily_bars(security.code, start=start or None,
                                          end=end or None, count=max(20, min(count, 3000)))
    if err or frame is None: return _wrap(None, err or "K线为空")
    rows = frame.copy(); rows["date"] = rows.date.dt.strftime("%Y-%m-%d")
    return _wrap({"security": security.to_dict(), "adjust": "QFQ_OR_PROVIDER_DEFAULT",
                  "items": rows.where(rows.notna(), None).to_dict("records")}, None)


@app.get("/api/securities/{code}/analysis")
def get_market_security_analysis(code: str):
    try: security = resolve_security(code)
    except ValueError as exc: return _wrap(None, str(exc))
    bars, err = market_data().daily_bars(security.code, count=600)
    if err or bars is None: return _wrap(None, err or "K线为空")
    if security.market == "CN":
        gate = cn_research.validate(bars, security.code)
        signal = cn_research.latest_signal(security.code, bars, gate)
        return _wrap({"security": security.to_dict(), "signal": signal,
                      "validation": gate, "execution_mode": "READ_ONLY_RESEARCH"}, None)
    return _wrap({"security": security.to_dict(), "signal": None,
                  "validation": {"status": "MARKET_ADAPTER_PENDING"}}, None)


@app.get("/api/positions")
def get_market_positions(market: str = ""):
    frame, err = market_data().positions(market or None)
    if err: return _wrap(None, err)
    if frame is None or frame.empty: return _wrap({"market": market.upper() or "ALL", "items": []}, None)
    if "qty" in frame.columns:
        frame = frame[pd.to_numeric(frame["qty"], errors="coerce").fillna(0) != 0].copy()
    rows = frame.where(frame.notna(), None).to_dict("records")
    return _wrap({"market": market.upper() or "ALL", "items": rows}, None)


@app.get("/api/research-assets")
def get_research_assets():
    """Expose observation status without adding assets to portfolio accounting."""
    return _wrap(research_assets.status(), None)


@app.get("/api/cn/candidates")
def get_cn_candidates(codes: str = ""):
    pool = [x.strip() for x in codes.split(",") if x.strip()] or None
    result = cn_research.scan(market_data(), pool)
    forward_ledger.record_status({"strategies": [
        {"id": item["strategy_id"], "name": f"A股趋势研究 {item['code']}",
         "as_of": item.get("as_of"), "action": item.get("action", "WAIT"),
         "reason": item.get("reason", ""), "signal": item}
        for item in result.get("candidates", [])
    ]})
    return _wrap(result, None)


@app.get("/api/cn/backtest/{code}")
def get_cn_backtest(code: str):
    try: security = resolve_security(code, "CN")
    except ValueError as exc: return _wrap(None, str(exc))
    if security.market != "CN": return _wrap(None, "该接口只接受A股代码")
    bars, err = market_data().daily_bars(security.code, count=3000)
    if err or bars is None: return _wrap(None, err or "K线为空")
    return _wrap({"security": security.to_dict(), "validation": cn_research.validate(bars, security.code),
                  "backtest": cn_research.backtest(bars, price_limit=cn_price_limit(security.code),
                                                    lot_size=cn_lot_size(security.code))}, None)


@app.get("/api/cn/events")
def get_cn_events():
    return _wrap(cn_research.load_event_candidates(), None)


@app.post("/api/cn/events/import")
async def import_cn_events(request: Request):
    """Import ContestTrade output. Imported candidates remain RESEARCH_ONLY."""
    try: payload = await request.json(); count = cn_research.save_event_candidates(payload)
    except (ValueError, json.JSONDecodeError) as exc: return _wrap(None, str(exc))
    return _wrap({"imported": count, "validation_status": "RESEARCH_ONLY"}, None)


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


@app.get("/strategy-center/status")
def get_strategy_center_status(refresh: int = 0):
    """Qualified-strategy dashboard. Read-only; never places an order."""
    try:
        status = strategy_center.get_status(client(), refresh=bool(refresh))
        forward_ledger.record_status(status)
        forward_ledger.record_supertrend_exit_shadow()
        return _wrap(status, None)
    except Exception as e:  # noqa: BLE001
        return _wrap(None, f"策略中心更新失败: {e}")


@app.get("/forward-ledger")
def get_forward_ledger(limit: int = 200):
    return _wrap(forward_ledger.dashboard(max(1, min(limit, 1000))), None)


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
        futu = client()
        pos, err = (futu.positions_market("HK") if hasattr(futu, "positions_market")
                    else futu.positions())
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
    research_data = westock_research.get_research(code, cl)

    evidence = _analysis_evidence(
        analysis=analysis_result,
        southbound_data=sb_data,
        buyback_data=bb_data,
        news_data=nw_data,
        capital_flow_data=cf_data,
        fundamentals_data=fund_data,
        research_data=research_data,
    )
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


def _price_alert_run():
    """注入调度器的价格报警函数：交易时段每轮检查 config.price_alert.list，到价则推企业微信。"""
    res, err = price_alert.evaluate_all(client(), CONFIG)
    if res and res.get("alerts_to_push"):
        pushed = notify.notify_alerts(
            [{"code": a["code"], "name": a["name"], "level": a["level"], "msg": a["msg"]}
             for a in res["alerts_to_push"]],
            _webhook(),
            prefix=f"{CONFIG.get('system', {}).get('notify_prefix', '')} 价格报警",
            funds_note=_funds_note(),
        )
        return {"pushed": pushed, "scan_type": "price_alert"}
    return {"pushed": 0, "scan_type": "price_alert"}


def _position_risk_run():
    """Scan actual positions for stop-loss/take-profit risk during HK hours."""
    result, error = monitor.monitor_positions(client(), technical=False)
    if error or not result:
        return {"pushed": 0, "scan_type": "position_risk", "error": error}
    positions_by_code = {str(item.get("code")): item
                         for item in (result.get("positions") or [])}
    alerts = []
    for original in result.get("alerts") or []:
        alert = dict(original)
        position = positions_by_code.get(str(alert.get("code"))) or {}
        qty = float(position.get("qty") or 0)
        market_value = float(position.get("market_val") or 0)
        if qty:
            reference_price = market_value / qty if market_value else 0.0
            alert["msg"] = (
                f"{alert.get('msg', '')}；实时持仓{qty:g}股，"
                f"参考价HK${reference_price:.3f}，市值约HK${market_value:,.2f}")
        alerts.append(alert)
    pushed = notify.notify_alerts(
        alerts, _webhook(),
        prefix=f"{CONFIG.get('system', {}).get('notify_prefix', '')} 持仓风险",
        funds_note=_funds_note(),
    ) if alerts else 0
    return {"pushed": pushed, "scan_type": "position_risk",
            "positions_checked": len(result.get("positions") or []),
            "alerts": len(alerts)}


def _formal_execution_payload() -> dict | None:
    """Build a time-aware reminder from the sole formal strategy source."""
    status = strategy_center.get_status(client(), refresh=False)
    xiaomi = next((item for item in status.get("strategies", [])
                   if item.get("id") == "xiaomi_trend_v1"), {})
    live_price = None
    snap, snap_err = client().market_snapshot(["HK.01810"])
    if not snap_err and snap is not None and not snap.empty:
        try:
            value = float(snap.iloc[0].get("last_price"))
            live_price = value if value == value else None
        except (TypeError, ValueError):
            pass
    option_review = None
    if (xiaomi.get("raw_action") or xiaomi.get("action")) == "BUY":
        account = dict(status.get("portfolio") or {})
        account["total_assets"] = min(float(account.get("total_assets") or 20_000), 20_000)
        account["cash"] = min(float(account.get("available_cash") or 0), 20_000)
        option_review, _ = option_mapper.analyze(
            client(), "HK.01810", "BUY", account,
            historical_gate_passed=bool(
                (CONFIG.get("xiaomi_options", {}) or {}).get("historical_gate_passed", False)))
    return execution_alerts.build(status, option_review=option_review, live_price=live_price)


def _formal_execution_run():
    """Push formal stock timing and explicit option rejection/review every phase."""
    notify.retry_outbox(_webhook())
    alert = _formal_execution_payload()
    if not alert:
        return {"pushed": 0, "scan_type": "formal_execution", "alert": None}
    pushed = notify.push_if_new(
        alert["fingerprint"], alert["message"], _webhook(), min_interval=86_400,
        title=alert["title"])
    return {"pushed": int(bool(pushed)), "scan_type": "formal_execution", "alert": alert}


def _start_intraday_scheduler():
    """Run formal execution reminders plus risk checks; no intraday entry invention."""
    intraday_scheduler.start(CONFIG.get("intraday", {}) or {},
                             [_formal_execution_run, _price_alert_run, _position_risk_run])


# 托管前端（/ 必须在 API 路由之后）
if os.path.isdir(FRONTEND_DIR):
    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")

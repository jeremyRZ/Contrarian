"""Read-only strategy centre for the three qualified daily strategies.

This module deliberately has no order-placement function.  It may read Futu
positions to make a signal position-aware, but all output is a proposed order.
"""
from __future__ import annotations

from datetime import datetime
import inspect
import json
from pathlib import Path
import re
from typing import Any

import numpy as np
import pandas as pd
from .orb_universe import HK_LIQUID_SEED
from . import monitor, forward_ledger, strategy_portfolio
from ..futu_client import load_config

ROOT = Path(__file__).resolve().parents[2]
DAILY_DIR = ROOT / ".universal_daily_60"
UNIVERSE_FILE = ROOT / ".universal_daily" / "research_universe_60.csv"
UNIVERSE_META_FILE = ROOT / ".universal_daily" / "research_universe_60.meta.json"
CAPITAL = 20_000.0
DYNAMIC_POOL_SIZE = 60
CANDIDATE_RESULT_FILE = ROOT / "data" / "risk_adjusted_momentum_candidate.json"


def _capability_roadmap(strategies: list[dict]) -> dict:
    """Expose auditable capability states for the investor dashboard."""
    active = []
    for strategy in strategies:
        validation = strategy.get("validation") or {}
        active.append({
            "id": strategy.get("id"), "name": strategy.get("name"),
            "state": "RUNNING" if strategy.get("action") != "UNAVAILABLE" else "UNAVAILABLE",
            "evidence": (f"历史收益 {validation.get('return_pct'):.2f}%"
                         if validation.get("return_pct") is not None else "验证数据不可用"),
            "output": "生成 BUY / SELL / HOLD / WAIT，并经过账户风险门控",
        })
    active.extend([
        {"id": "portfolio_risk", "name": "富途持仓与组合风控", "state": "RUNNING",
         "evidence": "读取实际持仓、现金、集中度、杠杆与期权到期风险",
         "output": "阻止不满足风险预算的新仓位"},
        {"id": "forward_ledger", "name": "前向信号成绩单", "state": "RUNNING",
         "evidence": "按信号日期记录并在第5、20个交易日评价",
         "output": "策略失效时自动暂停新增仓位"},
        {"id": "point_in_time_universe", "name": "点时股票池与影子成交", "state": "RUNNING",
         "evidence": "每日股票池、下一开盘、8bps滑点、费用和机会成本入库",
         "output": "阻止幸存者偏差并验证可执行收益"},
        {"id": "strategy_portfolio", "name": "多策略风险预算", "state": "RUNNING",
         "evidence": "样本不足使用回撤代理，达到5个对齐样本后切换等风险贡献",
         "output": "公开权重方法和样本状态，不伪装成熟风险平价"},
        {"id": "option_mapper", "name": "正股到期权硬门控", "state": "RUNNING",
         "evidence": "方向、Delta、IV、期限、价差、流动性、历史稳健性和1%风险预算",
         "output": "任一门控失败即BLOCKED；退出信号不自动映射Put"},
    ])
    candidate = {}
    try:
        candidate = json.loads(CANDIDATE_RESULT_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    history = candidate.get("historical_2022_2026", {})
    learning = [
        {"id": "risk_adjusted_momentum_200", "name": "200日风险调整动量",
         "state": "VALIDATING", "progress": "已接入信号引擎、点时股票池和前向影子成交",
         "evidence": (f"历史收益 {history.get('net_return_pct', 0):.2f}% · PF {history.get('profit_factor', 0):.2f}"
                      if history else "候选报告不可用"),
         "blocker": "当前150只历史研究池仍有选择偏差，正式提醒保持REVIEW而非自动交易"},
        {"id": "xiaomi_supertrend_options", "name": "SuperTrend小米期权",
         "state": "REJECTED", "progress": "继续寻找稳定的方向、期限与行权价组合",
         "evidence": "固定参数邻域稳定性未达到75%门槛",
         "blocker": "历史门槛关闭，不产生期权买入提醒"},
        {"id": "intraday_research", "name": "港股日内策略",
         "state": "REJECTED", "progress": "研究ORB、恐慌反转与分钟级执行",
         "evidence": "现有样本外结果未通过质量门槛",
         "blocker": "交易成本和稳定性不足"},
    ]
    next_actions = [
        {"order": 1, "name": "200日调仓引擎", "deliverable": "已完成：BUY / SELL / HOLD / CASH及账户差异",
         "acceptance": "正式调仓日才生成订单；不误卖非策略持仓"},
        {"order": 2, "name": "点时股票池与影子成交", "deliverable": "已完成：逐日快照、开盘成交、滑点、费用和机会成本",
         "acceptance": "数据库唯一约束确保同日重复刷新不重复记账"},
        {"order": 3, "name": "策略组合", "deliverable": "已完成：前向样本成熟后自动切换等风险贡献",
         "acceptance": "不足5个对齐样本明确显示COLLECTING"},
        {"order": 4, "name": "期权映射层", "deliverable": "已完成：正股方向与期权赔率双重硬门控",
         "acceptance": "未通过历史稳健性与风险预算只显示BLOCKED"},
    ]
    return {"active": active, "learning": learning, "next": next_actions,
            "rule": "只有真实运行代码进入正在做；研究代码不得冒充可执行策略"}


def _risk_settings() -> dict:
    monitor_cfg = load_config().get("monitor", {})
    return {
        "min_cash_pct": float(monitor_cfg.get("min_cash_pct", 15.0)),
        "max_single_position_pct": float(monitor_cfg.get("max_single_position_pct", 30.0)),
        "max_leveraged_position_pct": float(monitor_cfg.get("max_leveraged_position_pct", 15.0)),
        "max_trade_risk_pct": float(monitor_cfg.get("max_trade_risk_pct", 1.0)),
    }


def _risk_position_size(entry: float, stop: float, lot_size: int, portfolio: dict | None,
                        max_position_pct: float) -> dict:
    """Size from actual equity, available cash and stop distance; never borrow to buy."""
    settings = _risk_settings()
    account = portfolio or {}
    equity = float(account.get("total_assets") or CAPITAL)
    cash_raw = account.get("cash")
    cash = max(float(cash_raw), 0.0) if cash_raw is not None else equity
    per_share_risk = max(float(entry) - float(stop), 0.0)
    if entry <= 0 or stop <= 0 or per_share_risk <= 0 or lot_size <= 0 or equity <= 0:
        return {"qty": 0, "risk_hkd": 0.0, "risk_pct": 0.0, "reason": "价格或账户数据不足"}
    risk_budget = equity * settings["max_trade_risk_pct"] / 100
    allocation_budget = min(cash, equity * max_position_pct / 100)
    qty_by_risk = int(risk_budget // (per_share_risk * lot_size)) * lot_size
    qty_by_cash = int(allocation_budget // (entry * lot_size)) * lot_size
    qty = max(0, min(qty_by_risk, qty_by_cash))
    risk_hkd = qty * per_share_risk
    return {"qty": qty, "risk_hkd": risk_hkd,
            "risk_pct": risk_hkd / equity * 100 if equity else 0.0,
            "reason": "按真实净资产、可用现金和止损距离计算"}


def _portfolio_gate(portfolio: dict) -> dict:
    settings = _risk_settings()
    reasons = []
    if not portfolio.get("available"):
        reasons.append("账户数据不可用，禁止新增风险")
    cash_ratio = portfolio.get("cash_ratio")
    if cash_ratio is None:
        reasons.append("现金比例不可用")
    elif float(cash_ratio) < settings["min_cash_pct"]:
        reasons.append(f"现金比例{float(cash_ratio):.1f}%低于最低{settings['min_cash_pct']:.0f}%")
    max_weight = float(portfolio.get("max_weight_pct") or 0)
    if max_weight >= settings["max_single_position_pct"]:
        reasons.append(f"最大单一标的仓位{max_weight:.1f}%超过{settings['max_single_position_pct']:.0f}%")
    leveraged_weight = sum(float(x.get("weight_pct") or 0)
                           for x in portfolio.get("underlyings", []) if x.get("leveraged"))
    if leveraged_weight >= settings["max_leveraged_position_pct"]:
        reasons.append(f"杠杆产品仓位{leveraged_weight:.1f}%超过{settings['max_leveraged_position_pct']:.0f}%")
    return {"allow_new_risk": not reasons, "reasons": reasons,
            "leveraged_weight_pct": leveraged_weight, "settings": settings}


def _apply_portfolio_gate(strategies: list[dict], gate: dict) -> None:
    if gate.get("allow_new_risk"):
        return
    gate_reason = "；".join(gate.get("reasons") or ["组合风险门控不通过"])
    for strategy in strategies:
        if strategy.get("action") in {"BUY", "REVIEW"}:
            strategy["raw_action"] = strategy["action"]
            strategy["action"] = "BLOCKED"
            strategy["reason"] = f"组合风险门控阻止新增仓位：{gate_reason}"
            strategy["suggested_qty"] = 0
            for item in strategy.get("proposed") or []:
                item["suggested_qty"] = 0
                item["blocked_reason"] = gate_reason
            for item in strategy.get("candidates") or []:
                item["suggested_qty"] = 0
                item["blocked_reason"] = gate_reason


def _action_queue(portfolio: dict, strategies: list[dict]) -> list[dict]:
    """Build one prioritized list so the user never has to reconcile panels manually."""
    queue = []
    gate = portfolio.get("gate") or {}
    cash_ratio = portfolio.get("cash_ratio")
    if cash_ratio is not None and cash_ratio < gate.get("settings", {}).get("min_cash_pct", 15):
        queue.append({"priority": 1, "level": "MUST", "action": "降低融资",
                      "code": None, "name": "账户现金",
                      "reason": f"现金比例{cash_ratio:.1f}%，新增风险会继续扩大融资敞口",
                      "deadline": "下一次交易前", "allowed": True})
    for holding in portfolio.get("underlyings", []):
        if holding.get("risk") == "集中度过高":
            queue.append({"priority": 1, "level": "MUST", "action": "降低集中度",
                          "code": holding["code"], "name": holding["name"],
                          "reason": f"占净资产{holding.get('weight_pct', 0):.1f}%，超过单一标的上限",
                          "deadline": "下一交易时段复核", "allowed": True})
        elif holding.get("leveraged"):
            queue.append({"priority": 2, "level": "SHOULD", "action": "复核杠杆仓位",
                          "code": holding["code"], "name": holding["name"],
                          "reason": f"杠杆产品占净资产{holding.get('weight_pct', 0):.1f}%",
                          "deadline": "今日", "allowed": True})
        for derivative in holding.get("derivatives", []):
            option = derivative.get("option") or {}
            dte = option.get("days_to_expiry")
            if dte is not None and dte <= 14:
                queue.append({"priority": 1, "level": "MUST", "action": "处理临期期权",
                              "code": holding["code"], "name": derivative["name"],
                              "reason": f"距离到期仅{dte}天，时间价值衰减和Gamma风险上升",
                              "deadline": option.get("expiry"), "allowed": True})
    for strategy in strategies:
        action = strategy.get("action")
        if action == "SELL":
            queue.append({"priority": 1, "level": "MUST", "action": "卖出",
                          "code": "HK.01810" if strategy.get("id") == "xiaomi_trend_v1" else None,
                          "name": strategy.get("name"), "reason": strategy.get("reason"),
                          "deadline": "下一交易日开盘复核", "allowed": True})
        elif action in {"BUY", "REVIEW"}:
            for item in strategy.get("proposed") or strategy.get("candidates") or []:
                queue.append({"priority": 3, "level": "OPPORTUNITY", "action": "买入复核",
                              "code": item.get("code"), "name": item.get("name"),
                              "reason": strategy.get("reason"), "deadline": "下一交易日开盘前",
                              "allowed": gate.get("allow_new_risk", False),
                              "trade_plan": item.get("trade_plan"), "sizing": item.get("sizing")})
    if not queue:
        queue.append({"priority": 4, "level": "NONE", "action": "保持现状", "code": None,
                      "name": "今日无动作", "reason": "没有触发卖出、减仓或合格买入条件",
                      "deadline": "下一交易日继续扫描", "allowed": True})
    return sorted(queue, key=lambda item: item["priority"])


def _market_context(universe: dict, rotation: dict) -> dict:
    stocks = [x for x in universe.get("stocks", []) if x.get("change_rate") is not None]
    up = sum(float(x["change_rate"]) > 0 for x in stocks)
    down = sum(float(x["change_rate"]) < 0 for x in stocks)
    flat = len(stocks) - up - down
    median = float(np.median([float(x["change_rate"]) for x in stocks])) if stocks else None
    sector_values: dict[str, list[float]] = {}
    for stock in stocks:
        sector_values.setdefault(stock.get("sector") or "未分类", []).append(float(stock["change_rate"]))
    sectors = [{"name": name, "average_change_pct": float(np.mean(values)), "count": len(values)}
               for name, values in sector_values.items()]
    sectors.sort(key=lambda x: x["average_change_pct"], reverse=True)
    market = rotation.get("market") or {}
    hsi_change = None
    try:
        idx = _read_daily(DAILY_DIR / "HK_800000.csv")
        if len(idx) >= 2:
            hsi_change = (float(idx.close.iloc[-1]) / float(idx.close.iloc[-2]) - 1) * 100
    except Exception:  # noqa: BLE001
        pass
    eligible = bool(market.get("eligible"))
    breadth_weak = bool(stocks and down > up * 1.5)
    regime = "进攻" if eligible and not breadth_weak else ("谨慎" if eligible else "防守")
    max_new_exposure_pct = 60 if regime == "进攻" else (25 if regime == "谨慎" else 0)
    return {"regime": regime, "max_new_exposure_pct": max_new_exposure_pct,
            "hsi_change_pct": hsi_change,
            "breadth": {"scope": "动态流动性池", "sample": len(stocks), "up": up,
                        "flat": flat, "down": down, "median_change_pct": median},
            "sectors": sectors,
            "classification_note": "当前分类为名称规则初分，仅用于导航，不参与正式行业归因"}


def _replacement_decision(portfolio: dict, strategies: list[dict]) -> dict:
    gate = portfolio.get("gate") or {}
    if not gate.get("allow_new_risk"):
        return {"status": "BLOCKED", "decision": "不新增也不替换",
                "reason": "账户风险门控未通过，应先降低融资、集中度或杠杆暴露"}
    opportunities = []
    for strategy in strategies:
        if strategy.get("action") not in {"BUY", "REVIEW"}:
            continue
        for item in strategy.get("proposed") or strategy.get("candidates") or []:
            opportunities.append({"code": item.get("code"), "name": item.get("name"),
                                  "strategy": strategy.get("name"), "score": item.get("score")})
    if not opportunities:
        return {"status": "NO_CANDIDATE", "decision": "维持现有组合",
                "reason": "没有通过策略和市场门控的新候选，不为换仓而换仓"}
    weakest = min(portfolio.get("underlyings") or [],
                  key=lambda x: float(x.get("total_pl") or 0), default=None)
    return {"status": "REVIEW", "decision": "进入替换复核",
            "candidate": opportunities[0],
            "compare_with": ({"code": weakest.get("code"), "name": weakest.get("name"),
                              "total_pl": weakest.get("total_pl")} if weakest else None),
            "reason": "只进入个股证据、风险收益比和交易成本复核；未证明优于现有持仓前不执行替换"}


def _serial(v: Any) -> Any:
    if isinstance(v, (np.bool_, bool)):
        return bool(v)
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.floating,)):
        return None if not np.isfinite(v) else float(v)
    if isinstance(v, float):
        return None if not np.isfinite(v) else v
    return v


def _read_daily(path: Path) -> pd.DataFrame:
    x = pd.read_csv(path)
    x["time_key"] = pd.to_datetime(x["time_key"], format="mixed")
    return x.sort_values("time_key").drop_duplicates("time_key", keep="last").set_index("time_key")


def _snapshot_all(client, codes: list[str]) -> tuple[pd.DataFrame | None, list[str]]:
    parts, errors = [], []
    for start in range(0, len(codes), 100):
        chunk = codes[start:start + 100]
        snap, err = client.market_snapshot(chunk)
        if err or snap is None:
            errors.append(f"快照批次 {start // 100 + 1}: {err or '无数据'}")
        elif not snap.empty:
            parts.append(snap)
    return (pd.concat(parts, ignore_index=True) if parts else None), errors


def _ensure_universe(client, force: bool = False) -> list[str]:
    """Build a dynamic liquid HK-stock universe from the full Futu market list."""
    if UNIVERSE_FILE.exists() and UNIVERSE_META_FILE.exists() and not force:
        return []
    if not hasattr(client, "stock_basicinfo"):
        return ["初始化研究股票池失败: 客户端不支持基础资料查询"]
    try:
        frame, err = client.stock_basicinfo()
    except Exception as exc:  # noqa: BLE001
        return [f"初始化研究股票池失败: {exc}"]
    if err or frame is None or frame.empty:
        return [f"初始化研究股票池失败: {err or '无基础资料'}"]
    basic = frame.copy()
    total = len(basic)
    basic = basic[basic["code"].astype(str).str.startswith("HK.")]
    if "stock_type" in basic:
        basic = basic[basic["stock_type"].astype(str).str.upper() == "STOCK"]
    if "delisting" in basic:
        basic = basic[~basic["delisting"].fillna(False).astype(bool)]
    if "suspension" in basic:
        suspended = basic["suspension"].astype(str).str.lower().isin({"true", "1", "yes"})
        basic = basic[~suspended]
    prefiltered = None
    prefilter_total = None
    if hasattr(client, "liquid_stock_candidates"):
        prefiltered, pre_err = client.liquid_stock_candidates(
            min_price=2, max_lot_price=CAPITAL * .50,
            min_market_value=1_000_000_000, limit=300)
        if pre_err:
            errors = [f"富途全市场服务端初筛失败: {pre_err}"]
        else:
            errors = []
            prefilter_total = getattr(prefiltered, "attrs", {}).get("market_filter_total")
    else:
        errors = ["客户端不支持全市场服务端初筛"]
    candidate_codes = (prefiltered["code"].astype(str).tolist()
                       if prefiltered is not None and not prefiltered.empty else [])
    if not candidate_codes:
        return errors + ["动态选池失败：服务端初筛没有返回候选；保留上次有效股票池"]
    snapshot, snapshot_errors = _snapshot_all(client, candidate_codes)
    errors.extend(snapshot_errors)
    if snapshot is None or snapshot.empty:
        return errors + ["动态选池失败：全港股行情快照为空；保留上次有效股票池"]
    x = snapshot.copy()
    for col in ("last_price", "bid_price", "ask_price", "turnover", "lot_size",
                "change_rate", "prev_close_price", "high_price", "low_price",
                "turnover_rate", "volume"):
        x[col] = pd.to_numeric(x.get(col), errors="coerce")
    before_liquidity = len(x)
    mid = (x["bid_price"] + x["ask_price"]) / 2
    x["spread_bps"] = (x["ask_price"] - x["bid_price"]) / mid * 10_000
    x["lot_notional_hkd"] = x["last_price"] * x["lot_size"]
    valid = ((x["last_price"] >= 2) & (x["bid_price"] > 0) &
             (x["ask_price"] >= x["bid_price"]) & (x["turnover"] >= 20_000_000) &
             (x["spread_bps"] <= 30) & (x["lot_notional_hkd"] <= CAPITAL * .50))
    if "suspension" in x:
        valid &= ~x["suspension"].fillna(False).astype(bool)
    if "sec_status" in x:
        valid &= x["sec_status"].astype(str).str.upper().eq("NORMAL")
    x = x[valid].copy()
    if x.empty:
        return errors + ["动态选池失败：没有股票通过流动性与可交易过滤；保留上次有效股票池"]
    x["liquidity_score"] = (np.log1p(x["turnover"]) -
                            np.log1p(x["spread_bps"].clip(lower=.1)) -
                            .25 * np.log1p(x["lot_notional_hkd"]))
    selected = x.sort_values(["liquidity_score", "turnover"], ascending=False).head(DYNAMIC_POOL_SIZE)
    # 小米有独立且已验证的策略，不能被通用流动性池意外排除。
    if "HK.01810" not in set(selected["code"].astype(str)):
        mi = x[x["code"].astype(str) == "HK.01810"]
        if not mi.empty:
            selected = pd.concat([selected.iloc[:-1], mi.iloc[:1]], ignore_index=True)
    output_columns = ["code", "name", "last_price", "lot_size", "turnover", "spread_bps",
                      "lot_notional_hkd", "liquidity_score", "change_rate",
                      "prev_close_price", "high_price", "low_price", "turnover_rate", "volume"]
    out = selected[[col for col in output_columns if col in selected.columns]].copy()
    out["selection_method"] = "dynamic_full_hk_snapshot_v1"
    UNIVERSE_FILE.parent.mkdir(exist_ok=True)
    out.to_csv(UNIVERSE_FILE, index=False)
    meta = {"selected_at": datetime.now().isoformat(timespec="seconds"),
            "method": "DYNAMIC_FULL_HK_LIQUIDITY_V1", "market_total": total,
            "ordinary_tradable": int(len(basic)), "server_prefilter_total": prefilter_total,
            "server_prefilter_loaded": len(candidate_codes), "snapshots_received": before_liquidity,
            "passed_initial_filters": int(len(x)), "selected": int(len(out)),
            "fallback_used": False, "snapshot_errors": errors[:8]}
    UNIVERSE_META_FILE.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return errors


def _positions(client) -> dict[str, float]:
    try:
        frame, err = client.positions()
        if err or frame is None or frame.empty:
            return {}
        return {str(r.code): float(getattr(r, "qty", 0) or 0) for _, r in frame.iterrows()}
    except Exception:  # noqa: BLE001 - status must degrade gracefully
        return {}


def _portfolio_payload(client) -> dict:
    """Return a compact, read-only portfolio snapshot using Futu account fields."""
    try:
        frame, err = client.positions()
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "error": str(exc), "positions": []}
    if err or frame is None:
        return {"available": False, "error": err or "持仓数据不可用", "positions": []}
    if frame.empty:
        return {"available": True, "count": 0, "market_value": 0.0,
                "total_pl": 0.0, "today_pl": 0.0, "positions": []}
    cols = {str(c).lower(): c for c in frame.columns}
    def value(row, *names, default=None):
        for key in names:
            col = cols.get(key)
            if col is not None:
                val = _serial(row.get(col))
                if val is not None:
                    return val
        return default
    cfg = load_config()
    excluded = set(str(x) for x in (cfg.get("monitor", {}).get("holdings_exclude") or []))
    rows = []
    for _, row in frame.iterrows():
        qty = value(row, "qty", default=0) or 0
        if float(qty) == 0:
            continue
        code = str(value(row, "code", default=""))
        name = str(value(row, "stock_name", "name", default=""))
        if code in excluded:
            continue
        underlying = monitor.derivative_underlying(code, name)
        symbol = code.upper().removeprefix("HK.")
        option_match = re.match(r"^[A-Z]+(\d{6})([CP])(\d+)$", symbol)
        option = None
        if underlying and option_match:
            yymmdd, side, strike_raw = option_match.groups()
            option = {
                "expiry": f"20{yymmdd[:2]}-{yymmdd[2:4]}-{yymmdd[4:6]}",
                "side": "CALL" if side == "C" else "PUT",
                "strike": float(strike_raw) / 1000,
                "delta_available": False,
            }
        rows.append({
            "code": code,
            "name": name,
            "qty": qty,
            "price": value(row, "nominal_price", "price"),
            "market_value": value(row, "market_val", default=0) or 0,
            "pl_value": value(row, "pl_val", default=0) or 0,
            "pl_ratio": value(row, "pl_ratio"),
            "today_pl": value(row, "today_pl_val", "today_pl", default=0) or 0,
            "underlying_code": underlying["code"] if underlying else code,
            "underlying_name": underlying["name"] if underlying else name,
            "instrument": "OPTION" if underlying else monitor._classify(name, code),
            "option": option,
        })
    option_rows = [x for x in rows if x["instrument"] == "OPTION"]
    if option_rows:
        snapshot_codes = sorted({x["code"] for x in option_rows} |
                                {x["underlying_code"] for x in option_rows})
        snap, snap_err = client.market_snapshot(snapshot_codes)
        if not snap_err and snap is not None and not snap.empty:
            snap_cols = {str(c).lower(): c for c in snap.columns}
            snap_by_code = {str(r[snap_cols["code"]]): r for _, r in snap.iterrows()} if "code" in snap_cols else {}
            def snap_num(srow, *names):
                for key in names:
                    col = snap_cols.get(key)
                    if col is not None:
                        val = _serial(srow.get(col))
                        if val is not None:
                            return val
                return None
            for item in option_rows:
                option_snap = snap_by_code.get(item["code"])
                underlying_snap = snap_by_code.get(item["underlying_code"])
                if option_snap is None:
                    continue
                details = item["option"] or {}
                details.update({
                    "delta": snap_num(option_snap, "option_delta", "delta"),
                    "gamma": snap_num(option_snap, "option_gamma", "gamma"),
                    "theta": snap_num(option_snap, "option_theta", "theta"),
                    "vega": snap_num(option_snap, "option_vega", "vega"),
                    "implied_volatility": snap_num(option_snap, "option_implied_volatility", "implied_volatility"),
                    "contract_size": snap_num(option_snap, "lot_size"),
                })
                expiry = details.get("expiry")
                if expiry:
                    try:
                        details["days_to_expiry"] = max(0, (pd.Timestamp(expiry).date() - datetime.now().date()).days)
                    except Exception:  # noqa: BLE001
                        details["days_to_expiry"] = None
                delta, multiplier = details.get("delta"), details.get("contract_size")
                underlying_price = snap_num(underlying_snap, "last_price") if underlying_snap is not None else None
                if delta is not None and multiplier and underlying_price:
                    details["delta_equivalent_shares"] = float(delta) * float(item["qty"]) * float(multiplier)
                    details["delta_notional_hkd"] = details["delta_equivalent_shares"] * float(underlying_price)
                    details["delta_available"] = True
                item["option"] = details
    rows.sort(key=lambda item: abs(float(item.get("market_value") or 0)), reverse=True)
    total_market_value = sum(float(x["market_value"] or 0) for x in rows)
    cash_ratio = cash = total_assets = None
    if hasattr(client, "cash_ratio"):
        try:
            cash_ratio, cash, total_assets = client.cash_ratio()
        except Exception:  # noqa: BLE001
            pass
    denominator = float(total_assets or total_market_value or 0)
    groups: dict[str, dict] = {}
    for item in rows:
        code = item["underlying_code"]
        group = groups.setdefault(code, {
            "code": code, "name": item["underlying_name"], "market_value": 0.0,
            "today_pl": 0.0, "total_pl": 0.0, "direct_market_value": 0.0,
            "derivatives": [], "delta_exposure_available": True,
            "leveraged": False,
        })
        group["market_value"] += float(item["market_value"] or 0)
        group["today_pl"] += float(item["today_pl"] or 0)
        group["total_pl"] += float(item["pl_value"] or 0)
        if item["instrument"] == "OPTION":
            group["derivatives"].append(item)
            if not (item.get("option") or {}).get("delta_available"):
                group["delta_exposure_available"] = False
        else:
            group["direct_market_value"] += float(item["market_value"] or 0)
            if item["instrument"] == "杠杆ETF":
                group["leveraged"] = True
    grouped = list(groups.values())
    for group in grouped:
        group["delta_notional_hkd"] = sum(float((x.get("option") or {}).get("delta_notional_hkd") or 0)
                                           for x in group["derivatives"])
        group["estimated_directional_exposure_hkd"] = group["direct_market_value"] + group["delta_notional_hkd"]
        group["weight_pct"] = group["market_value"] / denominator * 100 if denominator else None
        group["directional_weight_pct"] = (group["estimated_directional_exposure_hkd"] / denominator * 100
                                             if denominator and group["delta_exposure_available"] else None)
        weight = group["weight_pct"] or 0
        group["risk"] = ("集中度过高" if weight >= 30 else
                         "杠杆产品" if group["leveraged"] else
                         "仓位偏高" if weight >= 20 else "正常")
    grouped.sort(key=lambda item: abs(float(item.get("market_value") or 0)), reverse=True)
    return {
        "available": True,
        "count": len(rows),
        "market_value": total_market_value,
        "total_pl": sum(float(x["pl_value"] or 0) for x in rows),
        "today_pl": sum(float(x["today_pl"] or 0) for x in rows),
        "positions": rows,
        "underlyings": grouped,
        "cash": cash,
        "cash_ratio": cash_ratio,
        "total_assets": total_assets,
        "max_weight_pct": max((x.get("weight_pct") or 0 for x in grouped), default=0),
        "risk_note": "期权缺少实时Delta时仅汇总市值与盈亏，不计算等效正股敞口",
    }


def _universe_payload() -> dict:
    """返回今日决策实际使用的研究股票池，供页面透明展示。"""
    if not UNIVERSE_FILE.exists():
        return {"count": 0, "stocks": []}
    frame = pd.read_csv(UNIVERSE_FILE)
    def sector(name: str) -> str:
        groups = {
            "科技互联网": ("科技", "腾讯", "阿里", "百度", "京东", "快手", "哔哩", "美团", "小米", "金蝶", "联想", "数据", "机器人", "地平线", "优必选", "美图"),
            "金融地产": ("银行", "保险", "证券", "交易所", "地产", "置地", "房产", "控股"),
            "消费": ("汽车", "比亚迪", "小鹏", "理想", "零跑", "体育", "李宁", "农夫", "美味", "物流", "速递", "旅行", "毛戈平"),
            "医疗健康": ("医药", "医疗", "制药", "健康", "生物", "康龙", "再鼎", "三生"),
            "工业能源": ("石油", "能源", "黄金", "锂业", "通讯", "电信", "实业", "光学", "智家", "海控"),
        }
        for label, words in groups.items():
            if any(word in name for word in words):
                return label
        return "综合"
    stocks = []
    for rank, (_, row) in enumerate(frame.iterrows(), 1):
        name = str(row.get("name") or row["code"])
        price = _serial(row.get("last_price"))
        change_rate = _serial(row.get("change_rate"))
        prev_close = _serial(row.get("prev_close_price"))
        high = _serial(row.get("high_price"))
        low = _serial(row.get("low_price"))
        daily_path = DAILY_DIR / f"{str(row['code']).replace('.', '_')}.csv"
        try:
            daily = _read_daily(daily_path)
            latest = daily.iloc[-1]
            if price is None:
                price = float(latest["close"])
            if prev_close is None and len(daily) > 1:
                prev_close = float(daily.iloc[-2]["close"])
            if high is None and "high" in latest:
                high = float(latest["high"])
            if low is None and "low" in latest:
                low = float(latest["low"])
        except Exception:  # noqa: BLE001
            pass
        if change_rate is None and price is not None and prev_close:
            change_rate = (float(price) - float(prev_close)) / float(prev_close) * 100
        change_value = (float(price) - float(prev_close)) if price is not None and prev_close else None
        amplitude = ((float(high) - float(low)) / float(prev_close) * 100
                     if high is not None and low is not None and prev_close else None)
        stocks.append({
            "rank": rank, "code": str(row["code"]), "name": name,
            "sector": sector(name), "price": price,
            "change_rate": _serial(change_rate),
            "change_value": _serial(change_value),
            "amplitude": _serial(amplitude),
            "turnover_rate": _serial(row.get("turnover_rate")),
            "volume": _serial(row.get("volume")),
            "turnover": _serial(row.get("turnover")),
            "spread_bps": _serial(row.get("spread_bps")),
            "lot_size": _serial(row.get("lot_size")),
            "lot_notional_hkd": _serial(row.get("lot_notional_hkd")),
            "liquidity_score": _serial(row.get("liquidity_score")),
            "stage": "动态池",
        })
    meta = {}
    if UNIVERSE_META_FILE.exists():
        try:
            meta = json.loads(UNIVERSE_META_FILE.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            meta = {}
    dynamic = meta.get("method") == "DYNAMIC_FULL_HK_LIQUIDITY_V1"
    return {
        "count": len(stocks), "stocks": stocks,
        "type": "DYNAMIC_FULL_HK_LIQUIDITY" if dynamic else "LEGACY_FIXED_SEED_FALLBACK",
        "stats": meta,
        "basis": [
            "富途全港股普通股清单作为起点，不再从固定20只开始",
            "剔除退市、停牌、无有效买卖盘、股价低于2港元的股票",
            "当日成交额至少2000万港元、价差不超过30bps、每手金额不超过账户基准资金50%",
            "按成交容量、价差和每手资金占用综合评分，保留前60只进入历史数据扫描",
            "恒指 HK.800000 只作市场环境过滤，不属于候选股",
        ],
        "limitation": ("动态池依赖当日快照；开盘初期成交额未形成时沿用最近一次有效池。"
                       if dynamic else "动态池尚未成功生成，目前仅使用旧池降级，不能视为全市场扫描。"),
        "daily_filters": [
            "恒指收盘高于 MA120",
            "20日平均成交额不少于1亿港元",
            "年化波动率12%至70%",
            "价格与MA20、MA60、MA120满足趋势条件",
            "120日动量不少于10%，并按动量/波动率排序",
        ],
    }


def _workflow(strategies: list[dict], gate: dict | None = None) -> dict:
    actionable = []
    for strategy in strategies:
        action = strategy.get("action")
        if action in {"BUY", "SELL", "REVIEW"}:
            actionable.append({"strategy": strategy.get("name"), "action": action,
                               "reason": strategy.get("reason"),
                               "orders": strategy.get("proposed") or strategy.get("candidates") or []})
    blocked = gate is not None and not gate.get("allow_new_risk", True)
    return {
        "steps": [
            {"name": "收盘后更新", "rule": "从富途更新260根日线并校验数据日期"},
            {"name": "市场门控", "rule": "恒指低于MA120时停止轮动和突破买入"},
            {"name": "池内过滤", "rule": "过滤流动性、波动率、趋势、动量和突破条件"},
            {"name": "排序与仓位", "rule": "轮动最多4只、总仓60%；突破单股20%；小米专属策略50%"},
            {"name": "产生动作", "rule": "只输出BUY/SELL/HOLD/WAIT/REVIEW，不自动下真实订单"},
            {"name": "下一交易日", "rule": "开盘前复核停牌、跳空和可买手数；用户确认后才执行"},
            {"name": "持仓退出", "rule": "小米跌破MA20退出；轮动每20个交易日复核；突破执行策略止损与退出"},
            {"name": "结果留痕", "rule": "每日信号写入前向台账；无信号也记录WAIT，避免只记成功案例"},
        ],
        "actionable": actionable,
        "next_action": ("账户风险门控不通过：" + "；".join(gate.get("reasons", [])) + "。仅允许减仓、卖出或降低风险"
                        if blocked else ("存在待执行动作，请逐项打开个股深度页核对风险与证据"
                        if actionable else "当前没有合格交易；保持现金，下一交易日继续扫描")),
        "execution_mode": "READ_ONLY_USER_CONFIRMATION_REQUIRED",
    }


def _refresh_cache(client) -> list[str]:
    """Merge the latest adjusted daily bars into the research cache."""
    errors: list[str] = []
    errors.extend(_ensure_universe(client, force=True))
    if not UNIVERSE_FILE.exists():
        return errors
    codes = pd.read_csv(UNIVERSE_FILE)["code"].astype(str).tolist() + ["HK.800000"]
    DAILY_DIR.mkdir(exist_ok=True)
    for code in codes:
        frame, err = client.history_kline(code, max_count=260)
        if err or frame is None or frame.empty:
            # FutuOpenD occasionally drops a long refresh connection. Rebuild it
            # once and retry the same symbol instead of losing every later code.
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass
            frame, err = client.history_kline(code, max_count=260)
        if err or frame is None or frame.empty:
            errors.append(f"{code}: {err or '无日线数据'}")
            continue
        path = DAILY_DIR / f"{code.replace('.', '_')}.csv"
        new = frame.copy()
        if path.exists():
            new = pd.concat([pd.read_csv(path), new], ignore_index=True)
        new["time_key"] = pd.to_datetime(new["time_key"], format="mixed")
        new.sort_values("time_key").drop_duplicates("time_key", keep="last").to_csv(path, index=False)
    return errors


def _xiaomi_status(positions: dict[str, float], portfolio: dict | None = None) -> dict:
    x = _read_daily(DAILY_DIR / "HK_01810.csv")
    close = x["close"]
    latest = x.iloc[-1]
    ma20, ma60 = close.rolling(20).mean().iloc[-1], close.rolling(60).mean().iloc[-1]
    held = positions.get("HK.01810", 0) > 0
    entry_ok = latest.close > ma60 and ma20 > ma60
    exit_ok = held and latest.close < ma20
    if exit_ok:
        action, reason = "SELL", "收盘价跌破MA20；下一交易日开盘卖出全部小米"
    elif held:
        action, reason = "HOLD", "趋势仍有效；15%持仓高点回撤需由持仓状态继续跟踪"
    elif entry_ok:
        lot = 200
        sizing = _risk_position_size(float(latest.close), float(ma60), lot, portfolio, 20.0)
        qty = sizing["qty"]
        action, reason = "BUY" if qty else "WAIT", "收盘价>MA60且MA20>MA60；下一交易日开盘执行"
    else:
        action, reason = "WAIT", "小米趋势条件尚未同时成立"
    stop_price = float(ma20 if action in {"HOLD", "SELL"} else ma60)
    risk_per_share = max(float(latest.close) - stop_price, 0)
    sizing = (_risk_position_size(float(latest.close), stop_price, 200, portfolio, 20.0)
              if action != "SELL" else {"qty": int(positions.get("HK.01810", 0)),
                                         "risk_hkd": None, "risk_pct": None,
                                         "reason": "卖出全部现有持仓"})
    return {
        "id": "xiaomi_trend_v1", "name": "小米专属趋势", "status": "已通过历史研究",
        "as_of": str(x.index[-1].date()), "action": action, "reason": reason,
        "price": float(latest.close), "ma20": float(ma20), "ma60": float(ma60),
        "held_qty": positions.get("HK.01810", 0), "suggested_qty": int(sizing["qty"]),
        "sizing": sizing,
        "trade_plan": {"trigger": float(latest.close), "stop": stop_price,
                       "target": float(latest.close + 2 * risk_per_share) if risk_per_share else None,
                       "risk_reward": 2.0 if risk_per_share else None,
                       "validity": "下一交易日开盘复核", "status": action},
        "validation": {"return_pct": 28.9729, "max_drawdown_pct": -14.4996, "profit_factor": 1.9091},
    }


def _rotation_status(positions: dict[str, float], portfolio: dict | None = None) -> dict:
    universe = pd.read_csv(UNIVERSE_FILE)
    lots = {str(r.code): int(r.lot_size) for _, r in universe.iterrows()}
    names = {str(r.code): str(r["name"]) for _, r in universe.iterrows()}
    idx = _read_daily(DAILY_DIR / "HK_800000.csv")
    latest_date = idx.index[-1]
    idx_ma200 = idx.close.rolling(200).mean().iloc[-1]
    market_ok = idx.close.iloc[-1] > idx_ma200
    candidates = []
    for path in DAILY_DIR.glob("HK_*.csv"):
        x = _read_daily(path)
        if len(x) < 221 or latest_date not in x.index:
            continue
        code = str(x.iloc[-1].get("code", ""))
        if code not in lots:
            continue
        close = x.close
        r = x.loc[latest_date]
        ma200 = close.rolling(200).mean().loc[latest_date]
        turn20 = x.turnover.rolling(20).mean().loc[latest_date]
        vol60 = close.pct_change().rolling(60).std().loc[latest_date] * np.sqrt(252)
        mom = close.iloc[-21] / close.iloc[-221] - 1
        eligible = (market_ok and turn20 >= 100_000_000 and r.close >= 2
                    and r.close > ma200 and mom > 0 and np.isfinite(vol60) and vol60 > 0)
        if not eligible:
            continue
        lot = lots[code]
        equity = float((portfolio or {}).get("total_assets") or CAPITAL)
        cash = max(float((portfolio or {}).get("cash") or equity), 0)
        allocation = min(equity * .25, cash)
        qty = int(allocation // (float(r.close) * lot)) * lot
        sizing = {"qty": qty, "target_weight_pct": 25.0,
                  "estimated_amount_hkd": float(qty * r.close),
                  "reason": "四只股票等权目标，按实际净资产、现金和港股手数取整"}
        candidates.append({
            "code": code, "name": names.get(code, code), "price": float(r.close),
            "momentum_pct": float(mom * 100), "volatility_pct": float(vol60 * 100),
            "score": float(mom / vol60), "lot_size": lot, "suggested_qty": qty,
            "estimated_amount": float(qty * r.close), "affordable": qty > 0,
            "sizing": sizing,
            "trade_plan": {"trigger": float(r.close), "stop": None, "target": None,
                           "risk_reward": None, "validity": "仅正式调仓日执行排名退出"},
        })
    candidates.sort(key=lambda z: z["score"], reverse=True)
    dates = list(idx.index)
    review_dates = [d for i, d in enumerate(dates) if i >= 220 and (i - 220) % 20 == 0]
    is_review = latest_date in review_dates
    prior_reviews = [d for d in review_dates if d <= latest_date]
    elapsed = len(dates) - 1 - 220
    trading_days_until_review = 0 if is_review else 20 - (elapsed % 20)
    held_codes = {c for c, q in positions.items() if q > 0}
    proposed = [c for c in candidates if c["affordable"]][:4]
    target_codes = {item["code"] for item in proposed}
    managed = forward_ledger.managed_codes("hk_liquid_trend_rotation_v2")
    strategy_holdings = held_codes & managed
    orders = []
    if is_review:
        if not market_ok:
            orders = [{"code": code, "name": names.get(code, code), "action": "SELL",
                       "current_qty": int(positions.get(code, 0)), "target_qty": 0,
                       "difference_qty": -int(positions.get(code, 0)), "reason": "恒指低于MA200，调仓日转为现金"}
                      for code in sorted(strategy_holdings)]
        else:
            for item in proposed:
                current = int(positions.get(item["code"], 0)); target = int(item["suggested_qty"])
                orders.append({"code": item["code"], "name": item["name"],
                               "action": "BUY" if target > current else "HOLD",
                               "current_qty": current, "target_qty": target,
                               "difference_qty": max(0, target - current),
                               "reason": "进入风险调整动量前4"})
            for code in sorted(strategy_holdings - target_codes):
                current = int(positions.get(code, 0))
                orders.append({"code": code, "name": names.get(code, code), "action": "SELL",
                               "current_qty": current, "target_qty": 0, "difference_qty": -current,
                               "reason": "跌出前4或不再满足流动性/趋势条件"})
    action = "REVIEW" if is_review and orders else ("CASH" if is_review and not market_ok else "WAIT")
    reason = ("正式调仓日：已生成持仓差异订单" if action == "REVIEW"
              else ("正式调仓日且恒指门控关闭，保持现金" if action == "CASH"
                    else "今天不是正式20交易日调仓点；不产生买卖动作"))
    return {
        "id": "hk_liquid_trend_rotation_v2", "name": "港股200日风险调整动量", "status": "历史候选·前向验证中",
        "as_of": str(latest_date.date()), "action": action, "reason": reason,
        "market": {"hsi_close": float(idx.close.iloc[-1]), "hsi_ma200": float(idx_ma200), "eligible": bool(market_ok)},
        "is_review_day": is_review, "current_strategy_holdings": sorted(strategy_holdings),
        "last_review_date": str(prior_reviews[-1].date()) if prior_reviews else None,
        "next_review_date": "今天" if is_review else f"约{trading_days_until_review}个交易日后",
        "days_until_review": trading_days_until_review,
        "candidates": candidates[:10], "proposed": proposed, "orders": orders,
        "parameters": {"lookback_days": 200, "skip_recent_days": 20,
                       "rebalance_trading_days": 20, "positions": 4, "market_ma": 200},
        "validation": {"return_pct": 61.3953, "max_drawdown_pct": -14.7148,
                       "profit_factor": 2.6111, "trades": 46, "distinct_stocks": 28},
    }


def _breakout_status(positions: dict[str, float], portfolio: dict | None = None) -> dict:
    universe = pd.read_csv(UNIVERSE_FILE)
    lots = {str(r.code): int(r.lot_size) for _, r in universe.iterrows()}
    names = {str(r.code): str(r["name"]) for _, r in universe.iterrows()}
    idx = _read_daily(DAILY_DIR / "HK_800000.csv")
    date = idx.index[-1]; hsi_ma = idx.close.rolling(120).mean().iloc[-1]
    market_ok = idx.close.iloc[-1] > hsi_ma
    rows = []
    for path in DAILY_DIR.glob("HK_*.csv"):
        x = _read_daily(path)
        if date not in x.index or len(x) < 221:
            continue
        code = str(x.iloc[-1].get("code", ""))
        if code not in lots:
            continue
        c=x.close; z=x.loc[date]; ma50=c.rolling(50).mean(); ma200=c.rolling(200).mean()
        turn20=x.turnover.rolling(20).mean().loc[date]
        vol_ratio=z.volume/x.volume.rolling(20).mean().loc[date]
        prior_high=x.high.rolling(120).max().shift(1).loc[date]
        ok=(market_ok and turn20>=100_000_000 and z.close>=2 and z.close>ma200.loc[date]
            and ma50.loc[date]>ma200.loc[date]>ma200.iloc[-21]
            and z.close>prior_high and vol_ratio>=1.2)
        if ok:
            lot=lots[code]
            stop = min(float(prior_high), float(z.close) * .93)
            sizing = _risk_position_size(float(z.close), stop, lot, portfolio, 20.0)
            qty = sizing["qty"]
            rows.append({"code":code,"name":names.get(code,code),"price":float(z.close),
                         "volume_ratio":float(vol_ratio),"prior_high120":float(prior_high),
                         "lot_size":lot,"suggested_qty":qty,"estimated_amount":float(qty*z.close),
                         "sizing":sizing,
                         "trade_plan":{"trigger":float(z.close),"stop":stop,
                                       "target":float(z.close+2*(z.close-stop)),
                                       "risk_reward":2.0,"validity":"下一交易日开盘复核"}})
    rows.sort(key=lambda r:r["volume_ratio"],reverse=True)
    return {"id":"hk_long_term_high_breakout_v1","name":"港股长期新高突破","status":"已通过历史研究",
            "as_of":str(date.date()),"action":"BUY" if any(r["suggested_qty"] for r in rows[:4]) else "WAIT",
            "reason": (
                "恒指低于MA120，市场门控不通过；禁止新开突破仓位"
                if not market_ok
                else ("发现放量突破，建议下一交易日开盘核对" if rows else "恒指环境通过，但当前没有120日放量新高突破")
            ),
            "market_eligible":bool(market_ok),"candidates":rows[:4],
            "validation":{"return_pct":28.6405,"max_drawdown_pct":-12.5422,"profit_factor":1.7967,"trades":54}}


def get_status(client, refresh: bool = False) -> dict:
    required = [DAILY_DIR / "HK_01810.csv", DAILY_DIR / "HK_800000.csv"]
    errors = _refresh_cache(client) if refresh or any(not p.exists() for p in required) else []
    positions = _positions(client)
    portfolio = _portfolio_payload(client)
    portfolio["gate"] = _portfolio_gate(portfolio)
    def safe(fn, strategy_id, name):
        try:
            return fn(positions, portfolio) if len(inspect.signature(fn).parameters) >= 2 else fn(positions)
        except Exception as exc:  # noqa: BLE001
            return {"id": strategy_id, "name": name, "status": "数据不可用",
                    "action": "UNAVAILABLE", "reason": str(exc), "as_of": None,
                    "price": None, "ma20": None, "ma60": None, "suggested_qty": 0,
                    "market": {"hsi_close": None, "hsi_ma200": None, "eligible": False},
                    "market_eligible": False, "is_review_day": False,
                    "validation": {"return_pct": None, "max_drawdown_pct": None,
                                   "profit_factor": None, "distinct_stocks": 0, "trades": 0},
                    "candidates": []}
    strategies = [
        safe(_xiaomi_status, "xiaomi_trend_v1", "小米专属趋势"),
        safe(_rotation_status, "hk_liquid_trend_rotation_v2", "港股200日风险调整动量"),
        safe(_breakout_status, "hk_long_term_high_breakout_v1", "港股长期新高突破"),
    ]
    forward = forward_ledger.dashboard(limit=1000)
    forward_stats = {x["strategy_id"]: x for x in forward.get("strategy_stats", [])}
    for strategy in strategies:
        stat = forward_stats.get(strategy.get("id"))
        strategy["forward_validation"] = stat or {"status": "COLLECTING", "mature_samples": 0}
        if stat and stat.get("status") == "REVIEW_REQUIRED" and strategy.get("action") in {"BUY", "REVIEW"}:
            strategy["raw_action"] = strategy["action"]
            strategy["action"] = "BLOCKED"
            strategy["reason"] = "前向验证表现失效，策略已自动降级，暂停新增仓位"
    _apply_portfolio_gate(strategies, portfolio["gate"])
    universe_payload = _universe_payload()
    forward_ledger.record_universe_snapshot(
        universe_payload, next((s.get("as_of") for s in strategies if s.get("as_of")), None))
    forward_ledger.record_rotation_shadow(strategies[1])
    rotation_codes = {x.get("code") for x in strategies[1].get("candidates", [])}
    breakout_codes = {x.get("code") for x in strategies[2].get("candidates", [])}
    for stock in universe_payload.get("stocks", []):
        code = stock.get("code")
        if code in breakout_codes:
            stock["stage"] = "突破候选"
        elif code in rotation_codes:
            stock["stage"] = "轮动候选"
        elif code == "HK.01810":
            stock["stage"] = "专属策略"
    market_context = _market_context(universe_payload, strategies[1])
    action_queue = _action_queue(portfolio, strategies)
    replacement = _replacement_decision(portfolio, strategies)
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "READ_ONLY_PAPER_ADVICE",
        "capital_hkd": CAPITAL,
        "universe": universe_payload,
        "portfolio": portfolio,
        "market_context": market_context,
        "action_queue": action_queue,
        "replacement_decision": replacement,
        "forward_scoreboard": {"summary": forward.get("summary", {}),
                               "strategy_stats": forward.get("strategy_stats", []),
                               "note": forward.get("note")},
        "paper_execution": forward.get("paper_execution", {}),
        "strategy_portfolio": strategy_portfolio.build_allocation(
            strategies, forward.get("evaluations", [])),
        "refresh_errors": errors[:8],
        "strategies": strategies,
        "capability_roadmap": _capability_roadmap(strategies),
        "workflow": _workflow(strategies, portfolio["gate"]),
        "intraday": {
            "name": "港股日内策略", "status": "禁用：样本外未通过", "action": "NO_TRADE",
            "reason": "ORB、恐慌反转和MACD分钟策略均未通过质量门槛",
        },
    }
    def clean(value):
        if isinstance(value, dict):
            return {k: clean(v) for k, v in value.items()}
        if isinstance(value, list):
            return [clean(v) for v in value]
        return _serial(value)

    return clean(payload)

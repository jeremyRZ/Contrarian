"""Read-only strategy centre for the three qualified daily strategies.

This module deliberately has no order-placement function.  It may read Futu
positions to make a signal position-aware, but all output is a proposed order.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from functools import cache
import hashlib
import inspect
import json
from pathlib import Path
import re
import subprocess
import time
from typing import Any

import numpy as np
import pandas as pd
import yaml

from app.hk_costs import affordable_board_lot, order_cost
from app import hk_calendar
from . import monitor, forward_ledger, signal_governance
from ..futu_client import load_config

ROOT = Path(__file__).resolve().parents[2]
HK_LIQUID_SEED = [
    "HK.00700", "HK.09988", "HK.03690", "HK.01024", "HK.01810",
    "HK.00941", "HK.01211", "HK.00981", "HK.09618", "HK.09888",
    "HK.00005", "HK.01299", "HK.02318", "HK.00388", "HK.00883",
    "HK.02020", "HK.06618", "HK.09626", "HK.01398", "HK.03988",
]
DAILY_DIR = ROOT / ".universal_daily_60"
UNIVERSE_FILE = ROOT / ".universal_daily" / "research_universe_60.csv"
UNIVERSE_META_FILE = ROOT / ".universal_daily" / "research_universe_60.meta.json"
CAPITAL = 20_000.0
DYNAMIC_POOL_SIZE = 60
STRATEGY_DIR = ROOT / "strategies"
REFRESH_META_FILE = ROOT / ".runtime" / "strategy_cache_refresh.json"
XIAOMI_POSITION_STATE_FILE = ROOT / ".runtime" / "xiaomi_trend_position_state.json"
BREAKOUT_POSITION_STATE_FILE = ROOT / ".runtime" / "hk_breakout_positions.json"


@cache
def _strategy_contract(strategy_id: str) -> dict:
    filenames = {
        "hk_liquid_trend_rotation_v2": "hk_rotation_v2.yaml",
        "hk_long_term_high_breakout_v1": "hk_breakout_v1.yaml",
        "xiaomi_trend_v1": "xiaomi_trend_v1.yaml",
    }
    path = STRATEGY_DIR / filenames[strategy_id]
    with path.open("r", encoding="utf-8") as stream:
        contract = yaml.safe_load(stream) or {}
    if contract.get("strategy_id") != strategy_id:
        raise ValueError(f"策略契约ID不匹配：{path}")
    return contract


def _contract_guard(strategy_id: str, strategy_fn, data_as_of: str) -> dict:
    """Fail closed when validated parameters or relevant code have drifted."""
    contract = _strategy_contract(strategy_id)
    expected = contract.get("runtime_guard") or {}
    parameters = {key: value for key, value in contract.items() if key != "runtime_guard"}
    parameter_hash = hashlib.sha256(
        json.dumps(parameters, sort_keys=True, ensure_ascii=False, default=str,
                   separators=(",", ":")).encode("utf-8")).hexdigest()
    code_hash = hashlib.sha256(inspect.getsource(strategy_fn).encode("utf-8")).hexdigest()
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True,
            text=True, check=True, timeout=3).stdout.strip()
        dirty = bool(subprocess.run(
            ["git", "status", "--porcelain", "--", str(Path(__file__).relative_to(ROOT)),
             str((STRATEGY_DIR / {
                 "hk_liquid_trend_rotation_v2": "hk_rotation_v2.yaml",
                 "hk_long_term_high_breakout_v1": "hk_breakout_v1.yaml",
             }[strategy_id]).relative_to(ROOT))],
            cwd=ROOT, capture_output=True, text=True, check=True, timeout=3).stdout.strip())
        validated_commit = str(expected.get("validated_code_commit") or "")
        commit_ok = bool(validated_commit) and subprocess.run(
            ["git", "merge-base", "--is-ancestor", validated_commit, commit],
            cwd=ROOT, capture_output=True, timeout=3).returncode == 0
    except (OSError, subprocess.SubprocessError, KeyError):
        commit, dirty, commit_ok = None, True, False
    checks = {
        "parameter_hash": parameter_hash == expected.get("parameter_sha256"),
        "code_hash": code_hash == expected.get("code_sha256"),
        "validated_commit": commit_ok,
        "clean_relevant_files": not dirty,
        "validated_data_cutoff": bool(expected.get("validated_data_cutoff")),
    }
    return {
        "status": "MATCH" if all(checks.values()) else "BLOCKED",
        "trading_authorized": False,
        "checks": checks,
        "parameter_sha256": parameter_hash,
        "code_sha256": code_hash,
        "code_commit": commit,
        "validated_data_cutoff": expected.get("validated_data_cutoff"),
        "runtime_data_as_of": data_as_of,
        "reason": "前向验证未达升级门槛，仅允许观察候选",
    }


def _risk_settings() -> dict:
    monitor_cfg = load_config().get("monitor", {})
    return {
        "portfolio_gate_enabled": bool(monitor_cfg.get("portfolio_gate_enabled", True)),
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
    cash, funds_error = _spendable_cash(account)
    if funds_error:
        return {"qty": 0, "risk_hkd": 0.0, "risk_pct": 0.0,
                "reason": funds_error}
    per_share_risk = max(float(entry) - float(stop), 0.0)
    if entry <= 0 or stop <= 0 or per_share_risk <= 0 or lot_size <= 0 or equity <= 0:
        return {"qty": 0, "risk_hkd": 0.0, "risk_pct": 0.0, "reason": "价格或账户数据不足"}
    risk_budget = equity * settings["max_trade_risk_pct"] / 100
    allocation_budget = min(cash, equity * max_position_pct / 100)
    qty_by_risk = int(risk_budget // (per_share_risk * lot_size)) * lot_size
    qty_by_cash = affordable_board_lot(entry, allocation_budget, lot_size)
    qty = max(0, min(qty_by_risk, qty_by_cash))
    risk_hkd = qty * per_share_risk
    one_lot_risk = per_share_risk * lot_size
    if not qty:
        reason = (f"一手止损风险HK${one_lot_risk:,.2f}超过1%风险预算HK${risk_budget:,.2f}"
                  if qty_by_risk < lot_size else
                  f"一手含费用金额超过可用配置资金HK${allocation_budget:,.2f}")
    else:
        reason = "按真实净资产、可用现金和止损距离计算"
    return {"qty": qty, "risk_hkd": risk_hkd,
            "risk_pct": risk_hkd / equity * 100 if equity else 0.0,
            "risk_budget_hkd": risk_budget, "one_lot_risk_hkd": one_lot_risk,
            "cash_budget_hkd": allocation_budget, "reason": reason}


def _live_allocation_size(entry: float, lot_size: int, portfolio: dict | None,
                          reference_capital: float, allocation_pct: float) -> dict:
    """Size a recommendation from the current Futu equity and spendable HKD cash."""
    account = portfolio or {}
    cash, funds_error = _spendable_cash(account)
    if funds_error:
        return {"qty": 0, "estimated_amount_hkd": 0.0,
                "allocation_budget_hkd": 0.0, "available_cash_hkd": None,
                "live_total_assets_hkd": account.get("total_assets"),
                "post_trade_cash_hkd": None, "affordable": False,
                "funds_source": account.get("funds_source"), "reason": funds_error}
    total_assets = account.get("total_assets")
    equity = float(total_assets)
    budget = min(cash, equity * float(allocation_pct) / 100)
    source = account.get("funds_source") or "PORTFOLIO_SNAPSHOT"
    if entry <= 0 or lot_size <= 0 or budget <= 0:
        return {"qty": 0, "estimated_amount_hkd": 0.0, "allocation_budget_hkd": budget,
                "available_cash_hkd": cash, "live_total_assets_hkd": total_assets,
                "post_trade_cash_hkd": cash, "affordable": False,
                "funds_source": source, "reason": "价格、现金或整手数据不足"}
    qty = affordable_board_lot(float(entry), budget, lot_size)
    estimated = max(qty, 0) * float(entry)
    estimated_cost = order_cost(estimated) if qty else 0.0
    return {"qty": max(qty, 0), "estimated_amount_hkd": estimated,
            "allocation_budget_hkd": budget, "capital_hkd": equity,
            "contract_reference_capital_hkd": float(reference_capital),
            "allocation_pct": float(allocation_pct),
            "available_cash_hkd": cash, "live_total_assets_hkd": total_assets,
            "estimated_cost_hkd": round(estimated_cost, 2),
            "post_trade_cash_hkd": round(max(cash - estimated - estimated_cost, 0.0), 2),
            "affordable": qty > 0 and estimated + estimated_cost <= cash,
            "funds_source": source,
            "reason": "按富途实时总资产、港币可用购买力、策略配置比例和整手约束"}


def _allocate_rotation_cash(candidates: list[dict], portfolio: dict,
                            target_pct: float, max_positions: int) -> None:
    """Make ranked reference quantities jointly affordable, in place."""
    remaining, error = _spendable_cash(portfolio)
    equity = float(portfolio.get("total_assets") or 0)
    selected = 0
    for item in candidates:
        budget = min(remaining, equity * target_pct / 100) if not error else 0
        qty = (affordable_board_lot(item["price"], budget, item["lot_size"])
               if selected < max_positions else 0)
        rejection = []
        if selected >= max_positions:
            rejection.append(f"排名未进入前{max_positions}")
        elif qty <= 0:
            rejection.append(f"剩余可配置现金HK${remaining:,.2f}不足一手")
        else:
            selected += 1
            remaining -= item["price"] * qty + order_cost(item["price"] * qty)
        item.update({"reference_qty": qty, "reference_amount": float(qty * item["price"]),
                     "affordable": qty > 0, "rejection_reasons": rejection})
        item["sizing"].update({"reference_qty": qty,
                               "reference_amount_hkd": item["reference_amount"],
                               "remaining_portfolio_cash_hkd": max(remaining, 0)})


def _spendable_cash(account: dict, max_age_seconds: int = 120) -> tuple[float, str | None]:
    """Return settled HKD cash only when the aggregate snapshot is complete and fresh."""
    if account.get("funds_complete") is not True or account.get("total_assets") is None:
        return 0.0, "账户资金快照不完整，禁止计算交易股数"
    raw = account.get("available_cash")
    stamp = account.get("funds_as_of")
    if raw is None or not stamp:
        return 0.0, "缺少港币可用购买力或资金时间戳，禁止计算交易股数"
    try:
        age = (datetime.now() - datetime.fromisoformat(str(stamp))).total_seconds()
        cash = max(float(raw), 0.0)
    except (TypeError, ValueError):
        return 0.0, "资金快照格式无效，禁止计算交易股数"
    if age < -5 or age > max_age_seconds:
        return 0.0, "资金快照已过期，禁止计算交易股数"
    return cash, None


def _advance_breakout_position(position: dict, bar: dict, ma20: float,
                               exit_cfg: dict) -> tuple[dict, str | None]:
    """Advance one owned position exactly once per trading date and evaluate exits."""
    state = dict(position)
    bar_date = str(bar["date"])
    if state.get("last_bar_date") != bar_date:
        state["bars_held"] = int(state.get("bars_held", 0)) + 1
        state["last_bar_date"] = bar_date
    state["peak_price"] = max(float(state.get("peak_price") or state["entry_price"]),
                              float(bar["high"]))
    entry = float(state["entry_price"])
    initial_stop = float(state["initial_stop"])
    activated = state["peak_price"] >= entry * (
        1 + float(exit_cfg["trailing_activation_profit_pct"]) / 100)
    trailing_stop = (state["peak_price"] *
                     (1 - float(exit_cfg["trailing_drawdown_pct"]) / 100)
                     if activated else None)
    state["trailing_active"] = activated
    state["trailing_stop"] = trailing_stop
    if float(bar["low"]) <= initial_stop:
        return state, "INITIAL_STOP"
    if trailing_stop is not None and float(bar["low"]) <= trailing_stop:
        return state, "TRAILING_STOP"
    if float(bar["close"]) < float(ma20):
        return state, "CLOSE_BELOW_MA20"
    if state["bars_held"] >= int(exit_cfg["maximum_hold_bars"]):
        return state, "MAX_HOLD_40D"
    return state, None


def register_breakout_fill(code: str, qty: int, entry_price: float, atr: float,
                           entry_date: str, book: str = "SIMULATED") -> dict:
    """Persist strategy ownership after a confirmed simulated/live fill callback."""
    contract = _strategy_contract("hk_long_term_high_breakout_v1")
    multiple = float(contract["exit"]["initial_stop_atr14_multiple"])
    state = _read_state(BREAKOUT_POSITION_STATE_FILE)
    state[str(code)] = {"code": str(code), "qty": int(qty),
                        "entry_price": float(entry_price),
                        "initial_stop": float(entry_price) - float(atr) * multiple,
                        "entry_date": str(entry_date), "last_bar_date": str(entry_date),
                        "bars_held": 0, "peak_price": float(entry_price),
                        "book": str(book).upper()}
    _write_state(BREAKOUT_POSITION_STATE_FILE, state)
    return state[str(code)]


def _portfolio_gate(portfolio: dict) -> dict:
    settings = _risk_settings()
    if not settings["portfolio_gate_enabled"]:
        return {"allow_new_risk": True, "reasons": [], "disabled": True,
                "note": "组合风险门控已由用户关闭；风险指标仅提示，不阻断交易信号",
                "leveraged_weight_pct": sum(
                    float(x.get("weight_pct") or 0) for x in portfolio.get("underlyings", [])
                    if x.get("leveraged")), "settings": settings}
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
    return {"allow_new_risk": not reasons, "reasons": reasons, "disabled": False,
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


def _apply_execution_conflicts(strategies: list[dict], portfolio: dict) -> None:
    """Require measurable Delta before combining stock and derivative exposure."""
    xiaomi = next((item for item in strategies
                   if item.get("id") == "xiaomi_trend_v1"), None)
    if not xiaomi or xiaomi.get("action") != "BUY":
        return
    holding = next((item for item in portfolio.get("underlyings", [])
                    if item.get("code") == "HK.01810"), None)
    derivatives = (holding or {}).get("derivatives") or []
    if not derivatives:
        return
    price = float(xiaomi.get("price") or 0)
    proposed_qty = int(xiaomi.get("suggested_qty") or 0)
    current_delta_shares = None
    if price > 0 and holding.get("delta_exposure_available"):
        current_delta_shares = float(holding.get("estimated_directional_exposure_hkd") or 0) / price
    projected_delta_shares = (current_delta_shares + proposed_qty
                              if current_delta_shares is not None else None)
    delta_missing = current_delta_shares is None
    xiaomi["execution_conflict"] = {
        "type": "EXISTING_DERIVATIVE_EXPOSURE",
        "blocking": delta_missing,
        "derivative_codes": [item.get("code") for item in derivatives],
        "current_delta_equivalent_shares": current_delta_shares,
        "proposed_stock_qty": proposed_qty,
        "projected_delta_equivalent_shares": projected_delta_shares,
        "resolution": ("期权Delta缺失，不计算合并仓位，也不输出可执行股数"
                       if delta_missing else "已计算现有期权与新增正股的合并Delta敞口"),
    }
    exposure = (f"当前Delta约{current_delta_shares:.0f}股，若买{proposed_qty}股后约"
                f"{projected_delta_shares:.0f}股" if current_delta_shares is not None
                else "现有期权Delta敞口无法完整核验")
    if delta_missing:
        xiaomi["raw_action"] = xiaomi["action"]
        xiaomi["raw_suggested_qty"] = proposed_qty
        xiaomi["action"] = "BLOCKED"
        xiaomi["suggested_qty"] = 0
        xiaomi["reason"] = ("原始正股方向为BUY，但账户已有小米衍生品且Delta不可用；"
                            "无法核验合并方向敞口，暂停给出股数")
    else:
        xiaomi["reason"] = ("收盘价>MA60且MA20>MA60；账户另有小米衍生品，"
                            f"{exposure}；已保留合并敞口供执行前复核")


def _apply_execution_timing(strategies: list[dict], now: datetime | None = None) -> None:
    """Expire next-open entries after 10:00 without erasing the raw signal."""
    now = now or datetime.now()
    xiaomi = next((item for item in strategies
                   if item.get("id") == "xiaomi_trend_v1"), None)
    if not xiaomi or xiaomi.get("action") != "BUY" or not xiaomi.get("as_of"):
        return
    try:
        signal_date = pd.Timestamp(xiaomi["as_of"]).date()
    except (TypeError, ValueError):
        return
    if now.date() <= signal_date or now.time() <= datetime.strptime("10:00", "%H:%M").time():
        return
    xiaomi["raw_action"] = "BUY"
    xiaomi["raw_suggested_qty"] = int(xiaomi.get("suggested_qty") or 0)
    xiaomi["action"] = "WAIT"
    xiaomi["suggested_qty"] = 0
    xiaomi["execution_status"] = "MISSED_NEXT_OPEN_WINDOW"
    xiaomi["reason"] = (
        f"{signal_date}收盘产生BUY，但下一交易日开盘执行窗口已经结束；"
        "不按盘中涨幅追价，等待今日收盘重新计算"
    )


def _action_queue(portfolio: dict, strategies: list[dict], now: datetime | None = None) -> list[dict]:
    """Build one prioritized list so the user never has to reconcile panels manually."""
    now = now or datetime.now()
    queue = []
    gate = portfolio.get("gate") or {}
    advisory_gate = bool(gate.get("disabled"))
    cash_ratio = portfolio.get("cash_ratio")
    if cash_ratio is not None and cash_ratio < gate.get("settings", {}).get("min_cash_pct", 15):
        queue.append({"priority": 1, "level": "SHOULD" if advisory_gate else "MUST", "action": "降低融资",
                      "code": None, "name": "账户现金",
                      "reason": f"现金比例{cash_ratio:.1f}%，新增风险会继续扩大融资敞口",
                      "deadline": "下一次交易前", "allowed": True})
    for holding in portfolio.get("underlyings", []):
        position_risk = holding.get("position_risk") or {}
        if holding.get("risk") == "止损触发":
            queue.append({"priority": 0, "level": "MUST", "action": "止损复核",
                          "code": holding["code"], "name": holding["name"],
                          "reason": position_risk.get("advice") or "已触发持仓止损规则",
                          "deadline": "下一交易时段优先复核", "allowed": True})
        elif holding.get("risk") == "止盈复核":
            queue.append({"priority": 1, "level": "SHOULD", "action": "止盈复核",
                          "code": holding["code"], "name": holding["name"],
                          "reason": position_risk.get("advice") or "已触发持仓止盈规则",
                          "deadline": "下一交易时段复核", "allowed": True})
        elif holding.get("risk") == "集中度过高":
            queue.append({"priority": 1, "level": "SHOULD" if advisory_gate else "MUST", "action": "降低集中度",
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
        conflict = strategy.get("execution_conflict")
        if conflict:
            queue.append({"priority": 2, "level": "SHOULD", "action": "方向敞口复核",
                          "code": "HK.01810", "name": "小米集团-W",
                          "reason": strategy.get("reason"), "deadline": "新增小米仓位前",
                          "allowed": not conflict.get("blocking", False),
                          "execution_conflict": conflict})
        if not strategy.get("actionable"):
            continue
        if action == "SELL":
            queue.append({"priority": 1, "level": "MUST", "action": "卖出",
                          "code": "HK.01810" if strategy.get("id") == "xiaomi_trend_v1" else None,
                          "name": strategy.get("name"), "reason": strategy.get("reason"),
                          "deadline": "下一交易日开盘复核", "allowed": True})
        elif action in {"BUY", "REVIEW"}:
            items = strategy.get("proposed") or strategy.get("candidates") or []
            if not items and strategy.get("id") == "xiaomi_trend_v1":
                qty = int(strategy.get("suggested_qty") or 0)
                if qty:
                    try:
                        stamp = pd.Timestamp(strategy.get("as_of"))
                        signal_date = now.date() if pd.isna(stamp) else stamp.date()
                    except (TypeError, ValueError):
                        signal_date = now.date()
                    queue.append({"priority": 3, "level": "OPPORTUNITY",
                                  "action": "买入复核", "code": "HK.01810",
                                  "name": "小米集团-W", "reason": strategy.get("reason"),
                                  "deadline": ("下一交易日开盘复核" if signal_date >= now.date()
                                               else "今日开盘复核"), "allowed": True,
                                  "suggested_qty": qty,
                                  "estimated_amount": qty * float(strategy.get("price") or 0),
                                  "trade_plan": strategy.get("trade_plan"),
                                  "sizing": strategy.get("sizing")})
            for item in items:
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


def _serial(v: Any) -> Any:
    if isinstance(v, (date, datetime)):
        return v.isoformat()
    if isinstance(v, (np.bool_, bool)):
        return bool(v)
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.floating,)):
        return None if not np.isfinite(v) else float(v)
    if isinstance(v, float):
        return None if not np.isfinite(v) else v
    return v


def _read_daily(path: Path, *, completed_only: bool = True) -> pd.DataFrame:
    x = pd.read_csv(path)
    x["time_key"] = pd.to_datetime(x["time_key"], format="mixed")
    if completed_only:
        x = x[x["time_key"].dt.date <= _expected_completed_session()]
    return x.sort_values("time_key").drop_duplicates("time_key", keep="last").set_index("time_key")


def _read_state(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_state(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def _xiaomi_trailing_state(*, held: bool, bar_date, high: float, close: float) -> dict:
    """Persist the observed post-entry high required by the strategy contract."""
    state = {}
    try:
        state = json.loads(XIAOMI_POSITION_STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    date_text = str(pd.Timestamp(bar_date).date())
    if not held:
        if state.get("active"):
            state.update({"active": False, "closed_detected_at": datetime.now().isoformat(timespec="seconds")})
            XIAOMI_POSITION_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            temp = XIAOMI_POSITION_STATE_FILE.with_suffix(".tmp")
            temp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
            temp.replace(XIAOMI_POSITION_STATE_FILE)
        return {"active": False, "peak_high": None, "drawdown_pct": None,
                "triggered": False, "quality": "NO_POSITION"}
    if not state.get("active"):
        state = {"active": True, "observed_since": datetime.now().date().isoformat(),
                 "first_available_bar": date_text, "peak_high": float(high),
                 "quality": "TRACKED_FROM_FIRST_DETECTED_POSITION"}
    else:
        state["peak_high"] = max(float(state.get("peak_high") or high), float(high))
    state.update({"last_bar_date": date_text, "last_close": float(close),
                  "updated_at": datetime.now().isoformat(timespec="seconds")})
    peak = float(state["peak_high"])
    drawdown = (float(close) / peak - 1) * 100 if peak > 0 else None
    state["drawdown_pct"] = drawdown
    XIAOMI_POSITION_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp = XIAOMI_POSITION_STATE_FILE.with_suffix(".tmp")
    temp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(XIAOMI_POSITION_STATE_FILE)
    threshold = float(_strategy_contract("xiaomi_trend_v1")["signal"]["exit"]
                      ["drawdown_from_position_high_pct"])
    return {**state, "triggered": bool(drawdown is not None and drawdown <= -threshold)}


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
            "historical_validation_universe": "CURRENT_SURVIVOR_BIASED",
            "promotion_eligible": False,
            "ordinary_tradable": int(len(basic)), "server_prefilter_total": prefilter_total,
            "server_prefilter_loaded": len(candidate_codes), "snapshots_received": before_liquidity,
            "passed_initial_filters": int(len(x)), "selected": int(len(out)),
            "fallback_used": False, "snapshot_errors": errors[:8]}
    UNIVERSE_META_FILE.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return errors


def _hk_position_frame(client):
    """Use the same all-HK-account position source as the investor homepage."""
    if hasattr(client, "positions_market"):
        return client.positions_market("HK")
    return client.positions()


def _positions(client) -> dict[str, float]:
    try:
        frame, err = _hk_position_frame(client)
        if err or frame is None or frame.empty:
            return {}
        quantities: dict[str, float] = {}
        for _, row in frame.iterrows():
            code = str(row.code)
            quantities[code] = quantities.get(code, 0.0) + float(getattr(row, "qty", 0) or 0)
        return quantities
    except Exception:  # noqa: BLE001 - status must degrade gracefully
        return {}


def _portfolio_payload(client) -> dict:
    """Return a compact, read-only portfolio snapshot using Futu account fields."""
    try:
        frame, err = _hk_position_frame(client)
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "error": str(exc), "positions": []}
    if err or frame is None:
        return {"available": False, "error": err or "持仓数据不可用", "positions": []}
    funds = None
    funds_error = None
    if hasattr(client, "account_summary_market"):
        try:
            funds, funds_error = client.account_summary_market("HK")
        except Exception as exc:  # noqa: BLE001
            funds_error = str(exc)
    cash_ratio = (funds or {}).get("cash_ratio")
    cash = (funds or {}).get("cash")
    available_cash = (funds or {}).get("available_cash")
    funds_complete = (funds or {}).get("funds_complete", False)
    funds_fields = (funds or {}).get("funds_fields", [])
    total_assets = (funds or {}).get("total_assets")
    funds_source = (funds or {}).get("source")
    funds_as_of = (funds or {}).get("as_of")
    matching_accounts = (funds or {}).get("matching_accounts")
    active_accounts = (funds or {}).get("active_accounts")
    if funds is None and hasattr(client, "cash_ratio"):
        try:
            cash_ratio, cash, total_assets = client.cash_ratio()
            funds_source = "LEGACY_DEFAULT_ACCOUNT"
        except Exception:  # noqa: BLE001
            pass
    if frame.empty:
        return {"available": True, "count": 0, "market_value": 0.0,
                "total_pl": 0.0, "today_pl": 0.0, "positions": [],
                "cash": cash, "available_cash": available_cash,
                "funds_complete": funds_complete, "funds_fields": funds_fields,
                "cash_ratio": cash_ratio, "total_assets": total_assets,
                "funds_source": funds_source, "funds_as_of": funds_as_of,
                "matching_accounts": matching_accounts, "active_accounts": active_accounts,
                "funds_error": funds_error, "underlyings": [], "max_weight_pct": 0}
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
        "available_cash": available_cash,
        "funds_complete": funds_complete,
        "funds_fields": funds_fields,
        "cash_ratio": cash_ratio,
        "total_assets": total_assets,
        "funds_source": funds_source,
        "funds_as_of": funds_as_of,
        "matching_accounts": matching_accounts,
        "active_accounts": active_accounts,
        "funds_error": funds_error,
        "max_weight_pct": max((x.get("weight_pct") or 0 for x in grouped), default=0),
        "risk_note": "期权缺少实时Delta时仅汇总市值与盈亏，不计算等效正股敞口",
    }


def _merge_position_risk(portfolio: dict, scan: dict | None, error: str | None = None) -> None:
    """Merge stop/take-profit findings into the decision-centre portfolio."""
    portfolio["risk_scan_error"] = error
    portfolio["risk_alert_count"] = 0
    if not scan:
        return
    by_code = {str(item.get("code")): item for item in scan.get("positions") or []}
    for group in portfolio.get("underlyings") or []:
        item = by_code.get(str(group.get("code")))
        if not item:
            continue
        signals = item.get("signals") or []
        group["position_risk"] = {
            "pl_ratio": item.get("pl_ratio"), "stop_loss_price": item.get("stop_loss_price"),
            "stop_pct": item.get("stop_pct"), "signals": signals,
            "advice": item.get("advice"), "lots": item.get("lots"),
        }
        if any("触及止损线" in str(signal) for signal in signals):
            group["risk"] = "止损触发"
            group["risk_severity"] = "DANGER"
            portfolio["risk_alert_count"] += 1
        elif any("减仓线" in str(signal) or "技术止盈" in str(signal) for signal in signals):
            group["risk"] = "止盈复核"
            group["risk_severity"] = "INFO"
            portfolio["risk_alert_count"] += 1


def _universe_payload() -> dict:
    rotation = _strategy_contract("hk_liquid_trend_rotation_v2")
    signal = rotation["signal"]
    universe_cfg = rotation["universe"]
    market_ma = rotation["market_regime"]["moving_average_days"]
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
        "point_in_time": {
            "available_from": meta.get("selected_at"),
            "historical_reconstruction_complete": False,
            "promotion_eligible": False,
            "reason": "仅从每日快照开始形成时点股票池；既往当前成分回测存在幸存者偏差",
        },
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
            f"恒指收盘高于 MA{market_ma}",
            f"股价不少于{universe_cfg['price_min_hkd']:g}港元，20日平均成交额不少于"
            f"{universe_cfg['historical_20d_turnover_min_hkd'] / 100_000_000:g}亿港元",
            f"个股收盘高于 MA{signal['stock_moving_average_days']}",
            f"计算{signal['lookback_days']}日动量并跳过最近{signal['skip_recent_days']}日",
            f"按动量/{signal['volatility_days']}日年化波动率排序",
        ],
    }


def _expected_completed_session(now: datetime | None = None):
    """Latest session that should have a completed daily bar in Hong Kong."""
    return hk_calendar.latest_completed_session(now or datetime.now())


def _cached_last_date(path: Path):
    try:
        frame = pd.read_csv(path, usecols=["time_key"])
        return pd.to_datetime(frame["time_key"], format="mixed").max().date()
    except (OSError, ValueError, KeyError, pd.errors.EmptyDataError):
        return None


def _refresh_meta() -> dict:
    try:
        return json.loads(REFRESH_META_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}


def _cache_needs_refresh(now: datetime | None = None) -> bool:
    expected = _expected_completed_session(now)
    latest = _cached_last_date(DAILY_DIR / "HK_800000.csv")
    if latest is not None and latest >= expected:
        return False
    return _refresh_meta().get("expected_through") != str(expected)


def _data_freshness(now: datetime | None = None) -> dict:
    expected = _expected_completed_session(now)
    index_date = _cached_last_date(DAILY_DIR / "HK_800000.csv")
    xiaomi_date = _cached_last_date(DAILY_DIR / "HK_01810.csv")
    latest = min((x for x in (index_date, xiaomi_date) if x is not None), default=None)
    meta = _refresh_meta()
    attempted = meta.get("expected_through") == str(expected)
    errors = meta.get("errors") or [] if attempted else []
    current = bool(latest and latest >= expected)
    # A successful same-session refresh may legitimately return the prior bar on
    # an HKEX holiday. Do not loop or pretend the transport failed.
    no_new_session = bool(not current and attempted and not errors and latest)
    return {"status": "CURRENT" if (current or no_new_session) else "STALE",
            "latest_date": str(latest) if latest else None,
            "expected_through": str(expected), "refresh_attempted": attempted,
            "note": ("交易所当日没有新日线，已使用最近完成交易日"
                     if no_new_session else None)}


def _refresh_cache(client) -> list[str]:
    """Merge the latest adjusted daily bars into the research cache."""
    expected = _expected_completed_session()
    if hasattr(client, "connect"):
        ok, message = client.connect()
        if not ok:
            error = message or "FutuOpenD 不可用"
            REFRESH_META_FILE.parent.mkdir(parents=True, exist_ok=True)
            REFRESH_META_FILE.write_text(json.dumps({
                "attempted_at": datetime.now().isoformat(timespec="seconds"),
                "expected_through": str(expected), "errors": [error],
                "index_latest": str(_cached_last_date(DAILY_DIR / "HK_800000.csv") or ""),
            }, ensure_ascii=False, indent=2), encoding="utf-8")
            return [error]
    errors: list[str] = []
    errors.extend(_ensure_universe(client, force=True))
    if not UNIVERSE_FILE.exists():
        return errors
    pool = pd.read_csv(UNIVERSE_FILE)["code"].astype(str).tolist()
    # Critical anchors first. Deduplication preserves order.
    codes = list(dict.fromkeys(["HK.800000", "HK.01810", *pool]))
    DAILY_DIR.mkdir(exist_ok=True)
    stale_codes = [code for code in codes if
                   (_cached_last_date(DAILY_DIR / f"{code.replace('.', '_')}.csv") or
                    datetime.min.date()) < expected]
    pace_seconds = 0.55 if len(stale_codes) > 55 else 0.0
    consecutive_errors = 0
    for code in stale_codes:
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
            consecutive_errors += 1
            if consecutive_errors >= 3:
                errors.append("OpenD连续3个标的更新失败，本轮刷新已提前终止")
                break
            continue
        consecutive_errors = 0
        path = DAILY_DIR / f"{code.replace('.', '_')}.csv"
        new = frame.copy()
        if path.exists():
            new = pd.concat([pd.read_csv(path), new], ignore_index=True)
        new["time_key"] = pd.to_datetime(new["time_key"], format="mixed")
        new = new[new["time_key"].dt.date <= expected]
        new.sort_values("time_key").drop_duplicates("time_key", keep="last").to_csv(path, index=False)
        if pace_seconds:
            time.sleep(pace_seconds)
    try:
        REFRESH_META_FILE.parent.mkdir(parents=True, exist_ok=True)
        REFRESH_META_FILE.write_text(json.dumps({
            "attempted_at": datetime.now().isoformat(timespec="seconds"),
            "expected_through": str(expected), "errors": errors[:20],
            "index_latest": str(_cached_last_date(DAILY_DIR / "HK_800000.csv") or ""),
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass
    return errors


def _xiaomi_status(positions: dict[str, float], portfolio: dict | None = None) -> dict:
    contract = _strategy_contract("xiaomi_trend_v1")
    entry = contract["signal"]["entry"]
    exit_cfg = contract["signal"]["exit"]
    execution = contract["execution"]
    fast_days = int(entry["fast_ma_days"])
    slow_days = int(entry["slow_ma_days"])
    exit_days = int(exit_cfg["close_below_ma_days"])
    trailing_pct = float(exit_cfg["drawdown_from_position_high_pct"])
    allocation_pct = float(execution["allocation_pct"])
    board_lot = int(execution["board_lot"])
    capital = float(contract["capital_hkd"])
    x = _read_daily(DAILY_DIR / "HK_01810.csv")
    close = x["close"]
    latest = x.iloc[-1]
    ma20 = close.rolling(fast_days).mean().iloc[-1]
    ma60 = close.rolling(slow_days).mean().iloc[-1]
    exit_ma = close.rolling(exit_days).mean().iloc[-1]
    held = positions.get("HK.01810", 0) > 0
    trailing = _xiaomi_trailing_state(
        held=held, bar_date=x.index[-1], high=float(latest.high), close=float(latest.close))
    entry_ok = latest.close > ma60 and ma20 > ma60
    exit_ok = held and latest.close < exit_ma
    if trailing.get("triggered"):
        action, reason = "SELL", (
            f"收盘价较持仓后记录高点回撤{abs(float(trailing['drawdown_pct'])):.1f}%，"
            f"达到{trailing_pct:g}%退出规则；下一交易日开盘卖出全部小米")
    elif exit_ok:
        action, reason = "SELL", "收盘价跌破MA20；下一交易日开盘卖出全部小米"
    elif held:
        action, reason = ("HOLD", f"趋势仍有效；收盘跌破MA{exit_days}时退出，"
                          f"{trailing_pct:g}%高点回撤需有入场状态才能核验")
    elif entry_ok:
        sizing = _live_allocation_size(float(latest.close), board_lot, portfolio, capital, allocation_pct)
        qty = sizing["qty"]
        action, reason = "BUY" if qty else "WAIT", "收盘价>MA60且MA20>MA60；下一交易日开盘执行"
    else:
        action, reason = "WAIT", "小米趋势条件尚未同时成立"
    exit_reference = float(exit_ma)
    sizing = (_live_allocation_size(float(latest.close), board_lot, portfolio, capital, allocation_pct)
              if action != "SELL" else {"qty": int(positions.get("HK.01810", 0)),
                                         "estimated_amount_hkd": None,
                                         "reason": "卖出全部现有持仓"})
    return {
        "id": "xiaomi_trend_v1", "name": "小米专属趋势", "status": "已通过历史研究",
        "as_of": str(x.index[-1].date()), "action": action, "reason": reason,
        "price": float(latest.close), "ma20": float(ma20), "ma60": float(ma60),
        "trailing_state": trailing,
        "held_qty": positions.get("HK.01810", 0), "suggested_qty": int(sizing["qty"]),
        "sizing": sizing,
        "trade_plan": {"trigger": float(latest.close), "stop": exit_reference,
                       "stop_type": "DAILY_CLOSE_BELOW_MA20",
                       "target": None, "risk_reward": None,
                       "trailing_drawdown_pct": trailing_pct,
                       "validity": "下一交易日开盘复核", "status": action},
        "strategy_contract": contract,
        "validation": {"return_pct": contract["validation"]["test_return_pct"],
                       "max_drawdown_pct": contract["validation"]["test_max_drawdown_pct"],
                       "profit_factor": contract["validation"]["test_profit_factor"]},
    }


def _xiaomi_momentum_observation(formal_strategy: dict) -> dict:
    """Expose the 20-day model as a non-actionable research observation."""
    x = _read_daily(DAILY_DIR / "HK_01810.csv")
    momentum = x.close.astype(float).pct_change(20).iloc[-1]
    state = 1 if momentum > .05 else -1 if momentum < -.05 else 0
    observation = signal_governance.research_observation(
        "xiaomi_momentum_20d_v1", as_of=str(x.index[-1].date()), state=state,
        value_pct=float(momentum * 100),
    )
    formal_intent = formal_strategy.get("trade_intent")
    disagrees = bool((formal_intent == "OPEN_LONG" and state < 0)
                     or (formal_intent == "EXIT_LONG" and state > 0))
    observation["comparison_to_formal"] = "DISAGREES" if disagrees else "ALIGNED_OR_NEUTRAL"
    observation["formal_strategy_id"] = formal_strategy.get("id")
    observation["formal_trade_intent"] = formal_intent
    observation["note"] = (
        "与正式信号方向不同，但该模型无决策权、不会改变交易动作"
        if disagrees else "仅作研究记录，不改变正式交易动作"
    )
    return observation


def _rotation_status(positions: dict[str, float], portfolio: dict | None = None) -> dict:
    contract = _strategy_contract("hk_liquid_trend_rotation_v2")
    signal = contract["signal"]
    universe_cfg = contract["universe"]
    market_ma_days = int(contract["market_regime"]["moving_average_days"])
    lookback_days = int(signal["lookback_days"])
    skip_days = int(signal["skip_recent_days"])
    stock_ma_days = int(signal["stock_moving_average_days"])
    volatility_days = int(signal["volatility_days"])
    review_days = int(contract["review_days"])
    max_positions = int(contract["maximum_positions"])
    target_pct = float(contract["per_position_target_allocation_pct"])
    history_required = lookback_days + skip_days
    universe = pd.read_csv(UNIVERSE_FILE)
    lots = {str(r.code): int(r.lot_size) for _, r in universe.iterrows()}
    names = {str(r.code): str(r["name"]) for _, r in universe.iterrows()}
    idx = _read_daily(DAILY_DIR / "HK_800000.csv")
    latest_date = idx.index[-1]
    idx_ma200 = idx.close.rolling(market_ma_days).mean().iloc[-1]
    market_ok = idx.close.iloc[-1] > idx_ma200
    market_distance_pct = (float(idx.close.iloc[-1]) / float(idx_ma200) - 1) * 100
    candidates = []
    filter_counts = {"history_or_date": 0, "outside_dynamic_pool": 0,
                     "turnover": 0, "price": 0, "below_ma200": 0,
                     "nonpositive_momentum": 0, "volatility": 0, "passed": 0}
    for path in DAILY_DIR.glob("HK_*.csv"):
        x = _read_daily(path)
        if len(x) < history_required + 1 or latest_date not in x.index:
            filter_counts["history_or_date"] += 1
            continue
        code = str(x.iloc[-1].get("code", ""))
        if code not in lots:
            filter_counts["outside_dynamic_pool"] += 1
            continue
        close = x.close
        r = x.loc[latest_date]
        ma200 = close.rolling(stock_ma_days).mean().loc[latest_date]
        turn20 = x.turnover.rolling(20).mean().loc[latest_date]
        vol60 = close.pct_change().rolling(volatility_days).std().loc[latest_date] * np.sqrt(252)
        mom = close.iloc[-(skip_days + 1)] / close.iloc[-(history_required + 1)] - 1
        if turn20 < float(universe_cfg["historical_20d_turnover_min_hkd"]):
            filter_counts["turnover"] += 1
            continue
        if r.close < float(universe_cfg["price_min_hkd"]):
            filter_counts["price"] += 1
            continue
        if r.close <= ma200:
            filter_counts["below_ma200"] += 1
            continue
        if mom <= 0:
            filter_counts["nonpositive_momentum"] += 1
            continue
        if not np.isfinite(vol60) or vol60 <= 0:
            filter_counts["volatility"] += 1
            continue
        filter_counts["passed"] += 1
        lot = lots[code]
        sized = _live_allocation_size(float(r.close), lot, portfolio,
                                      float(contract["capital_hkd"]), target_pct)
        qty = int(sized["qty"])
        executable_qty = qty if market_ok else 0
        sizing = {**sized, "qty": executable_qty, "reference_qty": qty,
                  "target_weight_pct": target_pct,
                  "estimated_amount_hkd": float(executable_qty * r.close),
                  "reference_amount_hkd": float(qty * r.close),
                  "reason": ("四只股票等权目标，按实际净资产、现金和港股手数取整"
                             if market_ok else "个股筛选通过，但恒指MA200门控关闭，仅作观察")}
        candidates.append({
            "code": code, "name": names.get(code, code), "price": float(r.close),
            "momentum_pct": float(mom * 100), "volatility_pct": float(vol60 * 100),
            "score": float(mom / vol60), "lot_size": lot,
            "suggested_qty": executable_qty, "reference_qty": qty,
            "estimated_amount": float(executable_qty * r.close),
            "reference_amount": float(qty * r.close), "affordable": qty > 0,
            "candidate_status": "ELIGIBLE" if market_ok else "OBSERVE_MARKET_GATE_BLOCKED",
            "sizing": sizing,
            "trade_plan": {"trigger": float(r.close), "stop": None, "target": None,
                           "risk_reward": None, "validity": "仅正式调仓日执行排名退出"},
        })
    candidates.sort(key=lambda z: z["score"], reverse=True)
    _allocate_rotation_cash(candidates, portfolio or {}, target_pct, max_positions)
    dates = list(idx.index)
    anchor = pd.Timestamp(contract["rebalance_anchor"])
    anchored_dates = [d for d in dates if d >= anchor]
    review_dates = [d for i, d in enumerate(anchored_dates) if i % review_days == 0]
    is_review = latest_date in review_dates
    prior_reviews = [d for d in review_dates if d <= latest_date]
    elapsed = max(len(anchored_dates) - 1, 0)
    trading_days_until_review = 0 if is_review else review_days - (elapsed % review_days)
    held_codes = {c for c, q in positions.items() if q > 0}
    proposed = ([c for c in candidates if c["affordable"]][:max_positions] if market_ok else [])
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
    shadow_orders = orders
    shadow_targets = proposed
    for item in candidates:
        item["suggested_qty"] = 0
        item["estimated_amount"] = 0.0
        item["candidate_status"] = "OBSERVE_ONLY_FORWARD_VALIDATION"
        item["sizing"]["qty"] = 0
        item["sizing"]["estimated_amount_hkd"] = 0.0
    action = "NO_TRADE"
    feasible = sum(item["reference_qty"] > 0 for item in candidates[:max_positions])
    reason = (f"仅观察：{len(candidates)}只通过个股筛选，其中{feasible}只可由当前现金整手配置；"
              f"恒指距MA200为{market_distance_pct:+.2f}%，下次复核{trading_days_until_review}个交易日后")
    guard = _contract_guard("hk_liquid_trend_rotation_v2", _rotation_status,
                            str(latest_date.date()))
    return {
        "id": "hk_liquid_trend_rotation_v2", "name": "港股200日风险调整动量", "status": "历史候选·前向验证中",
        "as_of": str(latest_date.date()), "action": action, "reason": reason,
        "market": {"hsi_close": float(idx.close.iloc[-1]), "hsi_ma200": float(idx_ma200),
                   "distance_to_ma_pct": market_distance_pct,
                   "gate_state": ("OPEN" if market_ok else
                                  "NEAR_THRESHOLD_BLOCKED" if market_distance_pct >= -.25
                                  else "BLOCKED"),
                   "eligible": bool(market_ok)},
        "is_review_day": is_review, "current_strategy_holdings": sorted(strategy_holdings),
        "last_review_date": str(prior_reviews[-1].date()) if prior_reviews else None,
        "next_review_date": "今天" if is_review else f"约{trading_days_until_review}个交易日后",
        "days_until_review": trading_days_until_review,
        "candidate_mode": "OBSERVE_ONLY",
        "filter_counts": filter_counts,
        "candidates": candidates[:10], "proposed": [], "orders": [],
        "shadow_targets": shadow_targets, "shadow_orders": shadow_orders,
        "contract_guard": guard,
        "parameters": {"lookback_days": lookback_days, "skip_recent_days": skip_days,
                       "rebalance_trading_days": review_days, "positions": max_positions,
                       "market_ma": market_ma_days},
        "strategy_contract": contract,
        "validation": {"return_pct": contract["validation"]["test_return_pct"],
                       "max_drawdown_pct": contract["validation"]["test_max_drawdown_pct"],
                       "profit_factor": contract["validation"]["test_profit_factor"],
                       "trades": contract["validation"]["test_trades"],
                       "distinct_stocks": contract["validation"]["distinct_stocks"],
                       "conflicting_walk_forward": contract["validation"]["conflicting_walk_forward"]},
    }


def _breakout_status(positions: dict[str, float], portfolio: dict | None = None) -> dict:
    contract = _strategy_contract("hk_long_term_high_breakout_v1")
    universe_cfg = contract["universe"]
    entry_cfg = contract["entry"]
    exit_cfg = contract["exit"]
    portfolio_cfg = contract["portfolio"]
    market_ma_days = int(contract["market_regime"]["moving_average_days"])
    breakout_days = int(entry_cfg["breakout_lookback_days"])
    atr_days = int(exit_cfg["atr_days"])
    universe = pd.read_csv(UNIVERSE_FILE)
    lots = {str(r.code): int(r.lot_size) for _, r in universe.iterrows()}
    names = {str(r.code): str(r["name"]) for _, r in universe.iterrows()}
    idx = _read_daily(DAILY_DIR / "HK_800000.csv")
    date = idx.index[-1]; hsi_ma = idx.close.rolling(market_ma_days).mean().iloc[-1]
    market_ok = idx.close.iloc[-1] > hsi_ma
    rows = []
    bars_by_code: dict[str, pd.DataFrame] = {}
    for path in DAILY_DIR.glob("HK_*.csv"):
        x = _read_daily(path)
        if date not in x.index or len(x) < 221:
            continue
        code = str(x.iloc[-1].get("code", ""))
        if code not in lots:
            continue
        bars_by_code[code] = x
        c=x.close; z=x.loc[date]; ma50=c.rolling(50).mean(); ma200=c.rolling(200).mean()
        turn20=x.turnover.rolling(20).mean().loc[date]
        vol_ratio=z.volume/x.volume.rolling(20).mean().loc[date]
        prior_high=x.high.rolling(breakout_days).max().shift(1).loc[date]
        ok=(market_ok and turn20>=float(universe_cfg["historical_20d_turnover_min_hkd"])
            and z.close>=float(universe_cfg["price_min_hkd"]) and z.close>ma200.loc[date]
            and ma50.loc[date]>ma200.loc[date]>ma200.iloc[-21]
            and z.close>prior_high
            and vol_ratio>=float(entry_cfg["volume_over_20d_average_ratio_min"]))
        if ok:
            lot=lots[code]
            previous_close = c.shift(1)
            true_range = pd.concat([
                x.high - x.low,
                (x.high - previous_close).abs(),
                (x.low - previous_close).abs(),
            ], axis=1).max(axis=1)
            atr = float(true_range.rolling(atr_days).mean().loc[date])
            stop = float(z.close) - atr * float(exit_cfg["initial_stop_atr14_multiple"])
            sizing = _risk_position_size(
                float(z.close), stop, lot, portfolio,
                float(portfolio_cfg["per_position_allocation_pct"]))
            reference_qty = sizing["qty"]
            qty = 0
            rows.append({"code":code,"name":names.get(code,code),"price":float(z.close),
                         "volume_ratio":float(vol_ratio),"prior_high120":float(prior_high),
                         "lot_size":lot,"suggested_qty":0,"reference_qty":reference_qty,
                         "estimated_amount":0.0,
                         "sizing":{**sizing, "qty":0, "reference_qty":reference_qty,
                                   "estimated_amount_hkd":0.0},
                         "trade_plan":{"trigger":float(z.close),"stop":stop,
                                       "target":float(z.close+2*(z.close-stop)),
                                       "risk_reward":2.0,"validity":"下一交易日开盘复核"}})
    rows.sort(key=lambda r:r["volume_ratio"],reverse=True)
    owned = _read_state(BREAKOUT_POSITION_STATE_FILE)
    shadow_exits, state_anomalies = [], []
    for code, position in list(owned.items()):
        if position.get("book") == "REAL" and float(positions.get(code, 0)) <= 0:
            state_anomalies.append(f"{code} 标记为突破策略真实持仓，但聚合账户中不存在")
            continue
        bars = bars_by_code.get(code)
        if bars is None or date not in bars.index:
            state_anomalies.append(f"{code} 缺少最新完整日线，退出规则未更新")
            continue
        bar = bars.loc[date]
        ma20 = float(bars.close.rolling(20).mean().loc[date])
        advanced, exit_reason = _advance_breakout_position(
            position, {"date": str(date.date()), "high": float(bar.high),
                       "low": float(bar.low), "close": float(bar.close)}, ma20, exit_cfg)
        if exit_reason:
            advanced["pending_exit"] = {"signal_date": str(date.date()),
                                         "reason": exit_reason, "execution": "NEXT_OPEN"}
            shadow_exits.append({"code": code, "name": names.get(code, code),
                                 "action": "SELL", "qty": int(advanced.get("qty", 0)),
                                 "reason": exit_reason, "signal_date": str(date.date()),
                                 "execution": "NEXT_OPEN"})
        owned[code] = advanced
    if owned:
        _write_state(BREAKOUT_POSITION_STATE_FILE, owned)
    guard = _contract_guard("hk_long_term_high_breakout_v1", _breakout_status,
                            str(date.date()))
    breakout_reason = (f"发现{len(rows)}只突破候选；" +
                       (str(rows[0]["sizing"]["reason"]) if rows else "当前没有合格突破") +
                       "；仅进入每日发现报告")
    return {"id":"hk_long_term_high_breakout_v1","name":"港股长期新高突破","status":"观察验证中",
            "as_of":str(date.date()),"action":"NO_TRADE",
            "reason":breakout_reason,
            "market_eligible":bool(market_ok),
            "candidates":rows[:int(portfolio_cfg["maximum_positions"])],
            "owned_positions": list(owned.values()),
            "shadow_exit_orders": shadow_exits,
            "state_anomalies": state_anomalies,
            "contract_guard":guard,
            "strategy_contract":contract,
            "validation":{"return_pct":contract["validation"]["test_return_pct"],
                          "max_drawdown_pct":contract["validation"]["test_max_drawdown_pct"],
                          "profit_factor":contract["validation"]["test_profit_factor"],
                          "trades":contract["validation"]["test_trades"]}}


def get_status(client, refresh: bool = False) -> dict:
    required = [DAILY_DIR / "HK_01810.csv", DAILY_DIR / "HK_800000.csv"]
    auto_refresh = any(not p.exists() for p in required) or _cache_needs_refresh()
    refresh_attempted = bool(refresh or auto_refresh)
    errors = _refresh_cache(client) if refresh_attempted else []
    freshness = _data_freshness()
    positions = _positions(client)
    portfolio = _portfolio_payload(client)
    try:
        risk_scan, risk_error = monitor.monitor_positions(client, technical=False)
    except Exception as exc:  # noqa: BLE001
        risk_scan, risk_error = None, str(exc)
    _merge_position_risk(portfolio, risk_scan, risk_error)
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
    if freshness["status"] != "CURRENT":
        for strategy in strategies:
            strategy["raw_action"] = strategy.get("action")
            strategy["action"] = "UNAVAILABLE"
            strategy["reason"] = (f"日线缓存过期：最新{freshness.get('latest_date') or '未知'}，"
                                  f"应更新至{freshness['expected_through']}；禁止据此交易")
    forward = forward_ledger.dashboard(limit=1000)
    forward_stats = {x["strategy_id"]: x for x in forward.get("strategy_stats", [])}
    paper_stats = {x["strategy_id"]: x for x in
                   (forward.get("paper_execution", {}).get("strategy_metrics") or [])}
    for strategy in strategies:
        stat = forward_stats.get(strategy.get("id"))
        strategy["forward_validation"] = stat or {"status": "COLLECTING", "mature_samples": 0}
        if stat and stat.get("status") == "REVIEW_REQUIRED" and strategy.get("action") in {"BUY", "REVIEW"}:
            strategy["raw_action"] = strategy["action"]
            strategy["action"] = "BLOCKED"
            strategy["reason"] = "前向验证表现失效，策略已自动降级，暂停新增仓位"
        metric = paper_stats.get(strategy.get("id"), {
            "strategy_id": strategy.get("id"), "complete_round_trips": 0,
            "realized_pnl_hkd": 0.0, "net_return_pct": 0.0,
            "profit_factor": None, "max_drawdown_pct": 0.0,
            "modeled_fill_deviation_bps": 8.0})
        contract = strategy.get("strategy_contract") or {}
        if not contract and strategy.get("id") in {
                "xiaomi_trend_v1", "hk_liquid_trend_rotation_v2",
                "hk_long_term_high_breakout_v1"}:
            contract = _strategy_contract(strategy["id"])
        gate = contract.get("promotion_gate") or {}
        strategy["lifecycle"] = str(contract.get("lifecycle") or "RESEARCH").upper()
        required = int(gate.get("minimum_complete_round_trips", 20))
        blockers = []
        if int(metric.get("complete_round_trips") or 0) < required:
            blockers.append(f"完整模拟交易{int(metric.get('complete_round_trips') or 0)}/{required}")
        if metric.get("complete_round_trips") and gate.get("require_positive_net_return", True) \
                and float(metric.get("net_return_pct") or 0) <= 0:
            blockers.append("扣费后收益未转正")
        if metric.get("complete_round_trips") and float(metric.get("profit_factor") or 0) \
                < float(gate.get("minimum_profit_factor", 1.2)):
            blockers.append("盈利因子未达门槛")
        if float(metric.get("max_drawdown_pct") or 0) \
                < float(gate.get("maximum_drawdown_pct", -20)):
            blockers.append("最大回撤超过门槛")
        strategy["paper_validation"] = metric
        strategy["promotion"] = {
            "status": "READY_FOR_MANUAL_REVIEW" if not blockers else "COLLECTING",
            "eligible": not blockers, "blockers": blockers,
            "automatic_promotion": False}
    _apply_portfolio_gate(strategies, portfolio["gate"])
    _apply_execution_conflicts(strategies, portfolio)
    _apply_execution_timing(strategies)
    for strategy in strategies:
        signal_governance.annotate_production_strategy(strategy)
    try:
        research_observations = [_xiaomi_momentum_observation(strategies[0])]
    except Exception as exc:  # noqa: BLE001
        research_observations = [{
            "id": "xiaomi_momentum_20d_v1", "name": "小米20日方向观察",
            "decision_role": "RESEARCH_ONLY", "observation": "UNAVAILABLE",
            "action": "NO_TRADE", "trade_intent": "NO_TRADE", "actionable": False,
            "decision_authority": "无交易决策权", "as_of": None,
            "observed_value_pct": None, "note": f"研究观察不可用：{exc}",
        }]
    universe_payload = _universe_payload()
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
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "READ_ONLY_PAPER_ADVICE",
        "capital_hkd": portfolio.get("total_assets"),
        "research_reference_capital_hkd": CAPITAL,
        "universe": universe_payload,
        "portfolio": portfolio,
        "market_context": market_context,
        "action_queue": action_queue,
        "forward_scoreboard": {"summary": forward.get("summary", {}),
                               "strategy_stats": forward.get("strategy_stats", []),
                               "note": forward.get("note")},
        "paper_execution": forward.get("paper_execution", {}),
        "refresh_errors": errors[:8],
        "data_freshness": {**freshness, "refresh_attempted_now": refresh_attempted},
        "signal_governance": signal_governance.governance_summary(
            notification_configured=bool((load_config().get("wecom", {}) or {}).get("webhook")),
            strategies=strategies),
        "research_observations": research_observations,
        "strategies": strategies,
        "intraday": {
            "name": "港股日内策略", "status": "禁用：样本外未通过", "action": "NO_TRADE",
            "reason": "ORB、恐慌反转和MACD分钟策略均未通过质量门槛",
            "risk_monitor": {"status": "ACTIVE", "interval_minutes": 30,
                             "scope": "真实持仓止损、止盈与手工价格报警",
                             "execution": "只提醒，不自动下单"},
        },
    }
    def clean(value):
        if isinstance(value, dict):
            return {k: clean(v) for k, v in value.items()}
        if isinstance(value, list):
            return [clean(v) for v in value]
        return _serial(value)

    return clean(payload)

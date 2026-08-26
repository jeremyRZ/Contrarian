"""Read-only Xiaomi CALL/PUT selector using a validated 55-day breakout."""
from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from . import xiaomi_directional
from .supertrend_research import SuperTrendParams, supertrend


def _num(row: pd.Series, name: str) -> float | None:
    value = row.get(name)
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if np.isfinite(value) else None


def rank_contracts(snapshot: pd.DataFrame, side: str, realized_vol_pct: float,
                   *, max_spread_pct: float = 12.0) -> list[dict]:
    """Return liquid, reasonably priced long-option candidates, best first."""
    rows = []
    target_delta = 0.45 if side == "CALL" else -0.45
    for _, row in snapshot.iterrows():
        if str(row.get("option_type", "")).upper() != side:
            continue
        bid, ask = _num(row, "bid_price"), _num(row, "ask_price")
        delta, iv = _num(row, "option_delta"), _num(row, "option_implied_volatility")
        volume = _num(row, "volume") or 0.0
        oi = _num(row, "option_open_interest") or 0.0
        if not bid or not ask or ask <= bid or delta is None or iv is None:
            continue
        mid = (bid + ask) / 2
        spread_pct = (ask - bid) / mid * 100
        iv_rv = iv / realized_vol_pct if realized_vol_pct > 0 else float("inf")
        if not (0.35 <= abs(delta) <= 0.60 and spread_pct <= max_spread_pct
                and volume >= 100 and oi >= 1_000 and iv_rv <= 1.15):
            continue
        score = (abs(delta - target_delta) * 100 + spread_pct + max(iv_rv - 0.8, 0) * 20)
        rows.append({"code": str(row.code), "side": side,
                     "strike": _num(row, "option_strike_price"),
                     "bid": bid, "ask": ask, "mid": mid, "spread_pct": spread_pct,
                     "iv_pct": iv, "realized_vol_20d_pct": realized_vol_pct,
                     "iv_to_realized": iv_rv, "delta": delta,
                     "theta": _num(row, "option_theta"), "vega": _num(row, "option_vega"),
                     "volume": int(volume), "open_interest": int(oi),
                     "contract_size": int(_num(row, "lot_size") or 0), "score": score})
    return sorted(rows, key=lambda x: x["score"])


def breakout_signal(bars: pd.DataFrame, lookback: int = 55) -> dict:
    """Signal known at the latest close; execution is for the next session."""
    x = bars.copy().sort_values("time_key").reset_index(drop=True)
    if len(x) <= lookback:
        return {"action": "WAIT", "reason": "55日突破数据不足"}
    latest = x.iloc[-1]
    prior = x.iloc[-lookback - 1:-1]
    high, low, close = float(prior.high.max()), float(prior.low.min()), float(latest.close)
    action = "BUY_CALL" if close > high else "BUY_PUT" if close < low else "WAIT"
    return {"action": action, "as_of": str(latest.time_key)[:10], "close": close,
            "prior_55d_high": high, "prior_55d_low": low,
            "reason": "收盘突破前55日高点" if action == "BUY_CALL" else
                      "收盘跌破前55日低点" if action == "BUY_PUT" else "未突破55日区间"}


def momentum_supertrend_signal(bars: pd.DataFrame, threshold: float = .05) -> dict:
    """Qualified option event: a new 20-day momentum state confirmed by ST(7,2.5)."""
    x = bars.copy().sort_values("time_key").reset_index(drop=True)
    if len(x) < 30:
        return {"action": "WAIT", "reason": "方向数据不足"}
    momentum = pd.to_numeric(x.close, errors="coerce").pct_change(20)
    trend = supertrend(x, SuperTrendParams(7, 2.5))["st_direction"]

    def state(value: float) -> int:
        return 1 if value > threshold else -1 if value < -threshold else 0

    current, previous = state(float(momentum.iloc[-1])), state(float(momentum.iloc[-2]))
    confirmed = current != 0 and current == int(trend.iloc[-1])
    transition = current != previous
    action = ("BUY_CALL" if current == 1 else "BUY_PUT"
              if current == -1 else "WAIT") if confirmed and transition else "WAIT"
    return {"action": action, "as_of": str(x.iloc[-1].time_key)[:10],
            "close": float(x.iloc[-1].close), "momentum_20d_pct": float(momentum.iloc[-1] * 100),
            "supertrend_direction": int(trend.iloc[-1]), "transition": transition,
            "reason": "20日动量状态切换且SuperTrend同向" if action != "WAIT" else
                      "动量未切换或SuperTrend未确认，不买期权"}


def rank_convex_contracts(snapshot: pd.DataFrame, side: str, spot: float,
                          *, target_otm_pct: float = 10.0,
                          max_spread_pct: float = 25.0,
                          max_target_error_pct: float = 5.0) -> list[dict]:
    """Rank liquid OTM contracts matching the out-of-sample-tested structure."""
    target = spot * (1 + target_otm_pct / 100) if side == "CALL" else spot * (1 - target_otm_pct / 100)
    rows = []
    for _, row in snapshot.iterrows():
        if str(row.get("option_type", "")).upper() != side:
            continue
        bid, ask = _num(row, "bid_price"), _num(row, "ask_price")
        volume = _num(row, "volume") or 0
        oi = _num(row, "option_open_interest") or 0
        strike = _num(row, "option_strike_price")
        delta = _num(row, "option_delta")
        if not bid or not ask or ask <= bid or strike is None or volume < 10 or oi < 100:
            continue
        mid = (bid + ask) / 2
        spread_pct = (ask - bid) / mid * 100
        target_error_pct = abs(strike / target - 1) * 100
        if (spread_pct > max_spread_pct or target_error_pct > max_target_error_pct
                or delta is None or not .15 <= abs(delta) <= .40):
            continue
        rows.append({"code": str(row.code), "side": side, "strike": strike,
                     "bid": bid, "ask": ask, "mid": mid, "spread_pct": spread_pct,
                     "iv_pct": _num(row, "option_implied_volatility"),
                     "delta": delta, "theta": _num(row, "option_theta"),
                     "vega": _num(row, "option_vega"), "volume": int(volume),
                     "open_interest": int(oi), "contract_size": int(_num(row, "lot_size") or 0),
                     "target_strike": target, "target_error_pct": target_error_pct,
                     "distance_to_target": abs(strike - target)})
    return sorted(rows, key=lambda item: (item["distance_to_target"], item["spread_pct"]))


def analyze(client, cfg: dict | None = None) -> tuple[dict | None, str | None]:
    settings = (cfg or {}).get("xiaomi_options", {}) or {}
    if not bool(settings.get("enabled", True)):
        return None, "小米期权推荐已禁用"
    direction, err = xiaomi_directional.live_status(client, cfg)
    if err or direction is None:
        return None, err or "正股方向不可用"
    base = {"id": "xiaomi_option_selector_v1", "as_of": direction["as_of"],
            "underlying": direction, "action": "NO_TRADE", "instrument": "NONE",
            "decision_role": "RESEARCH_ONLY", "actionable": False,
            "decision_authority": "无交易决策权",
            "recommendation": "正股方向处于中性区，不买Call或Put", "contract": None}
    end = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=180)).strftime("%Y-%m-%d")
    bars, bar_err = client.history_kline("HK.01810", max_count=160, start=start, end=end)
    if bar_err or bars is None or len(bars) < 57:
        return None, bar_err or "55日突破数据不足"
    option_signal = momentum_supertrend_signal(bars)
    base["option_signal"] = option_signal
    if option_signal["action"] == "WAIT":
        base["recommendation"] = "动量状态未切换或SuperTrend未确认，不买Call或Put"
        return base, None
    side = "CALL" if option_signal["action"] == "BUY_CALL" else "PUT"
    realized = float(pd.Series(bars.close, dtype=float).pct_change().tail(20).std() * np.sqrt(252) * 100)
    expiries, expiry_err = client.option_expiration_dates("HK.01810")
    if expiry_err or expiries is None or expiries.empty:
        return None, expiry_err or "期权到期日不可用"
    min_dte = int(settings.get("min_days_to_expiry", 30))
    max_dte = int(settings.get("max_days_to_expiry", 60))
    valid = expiries[(expiries.option_expiry_date_distance >= min_dte)
                     & (expiries.option_expiry_date_distance <= max_dte)].sort_values("option_expiry_date_distance")
    if valid.empty:
        base.update({"instrument": "NONE", "action": "NO_TRADE",
                     "recommendation": "没有25–75天合约；研究观察不转换为正股交易动作"})
        return base, None
    expiry = str(valid.iloc[0].strike_time)
    dte = int(valid.iloc[0].option_expiry_date_distance)
    chain, chain_err = client.option_chain("HK.01810", expiry, expiry)
    if chain_err or chain is None or chain.empty:
        return None, chain_err or "期权链不可用"
    codes = chain.loc[chain.option_type.astype(str).str.upper() == side, "code"].astype(str).tolist()
    snap, snap_err = client.market_snapshot(codes)
    if snap_err or snap is None or snap.empty:
        return None, snap_err or "期权报价不可用"
    candidates = rank_convex_contracts(
        snap, side, option_signal["close"],
        target_otm_pct=float(settings.get("target_otm_pct", 10)),
        max_spread_pct=float(settings.get("max_spread_pct", 25)))
    if not candidates:
        base.update({"instrument": "NONE", "action": "NO_TRADE", "expiry": expiry,
                     "days_to_expiry": dte, "realized_vol_20d_pct": realized,
                     "recommendation": f"{side}没有通过虚值距离、价差与流动性门槛；不买期权"})
        return base, None
    for item in candidates:
        item["expiry"] = expiry
        item["days_to_expiry"] = dte
        item["realized_vol_20d_pct"] = realized
        item["max_loss_per_contract_hkd"] = item["ask"] * item["contract_size"]
        item["expiry_break_even"] = (item["strike"] + item["ask"] if side == "CALL"
                                      else item["strike"] - item["ask"])
    historical_gate = bool(settings.get("historical_gate_passed", False))
    if not historical_gate:
        base.update({"instrument": "NONE", "action": "NO_TRADE",
                     "option_candidate": candidates[0],
                     "research_gate": {"passed": False,
                                       "reason": "固定参数的邻域稳定性未达到研究门槛"},
                     "recommendation": f"{side}出现方向候选，但稳健性门槛未通过；不买期权"})
        return base, None
    risk_pct = float(settings.get("max_premium_risk_pct", 1.0))
    cash_ratio = cash = total_assets = None
    if hasattr(client, "cash_ratio"):
        try:
            cash_ratio, cash, total_assets = client.cash_ratio()
        except Exception:  # noqa: BLE001
            pass
    if total_assets is None:
        base.update({"instrument": "NONE", "action": "NO_TRADE",
                     "option_candidate": candidates[0],
                     "recommendation": f"{side}通过市场门槛，但账户总资产不可用，风险预算无法验证；优先正股"})
        return base, None
    risk_budget = float(total_assets) * risk_pct / 100
    affordable = [item for item in candidates
                  if item["max_loss_per_contract_hkd"] <= risk_budget
                  and item["max_loss_per_contract_hkd"] <= float(cash or 0)]
    if not affordable:
        base.update({"instrument": "NONE", "action": "NO_TRADE",
                     "option_candidate": candidates[0],
                     "risk_gate": {"total_assets_hkd": float(total_assets),
                                   "cash_hkd": float(cash or 0), "max_loss_pct": risk_pct,
                                   "max_loss_budget_hkd": risk_budget, "passed": False},
                     "recommendation": f"{side}通过市场门槛，但一张合约超过单笔最大损失预算；当前优先正股"})
        return base, None
    best = affordable[0]
    base["risk_gate"] = {"total_assets_hkd": float(total_assets), "cash_hkd": float(cash or 0),
                         "max_loss_pct": risk_pct, "max_loss_budget_hkd": risk_budget,
                         "passed": True, "max_contracts": int(risk_budget // best["max_loss_per_contract_hkd"])}
    base.update({"instrument": side, "action": "NO_TRADE",
                 "observed_action": f"BUY_{side}", "contract": best,
                 "recommendation": f"{side}通过研究门槛，但研究模型没有交易决策权；仅记录候选，不生成买入建议"})
    return base, None


def notification(result: dict) -> tuple[str, str] | None:
    """Research option selectors are structurally unable to notify trades."""
    return None

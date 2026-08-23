"""Read-only stock-signal to HK option mapping with hard value/risk gates."""
from __future__ import annotations

from datetime import datetime, timedelta
import numpy as np
import pandas as pd

from .xiaomi_options import rank_contracts


def analyze(client, code: str, stock_action: str, portfolio: dict | None = None,
            historical_gate_passed: bool = False) -> tuple[dict, str | None]:
    base = {"underlying": code, "stock_action": stock_action, "action": "BLOCKED",
            "contract": None, "term_structure": [], "gates": {}}
    if stock_action != "BUY":
        base["reason"] = "正股没有新增多头信号；退出信号不等同看空，不映射Put"
        return base, None
    end = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=220)).strftime("%Y-%m-%d")
    bars, err = client.history_kline(code, max_count=180, start=start, end=end)
    if err or bars is None or len(bars) < 30:
        return base, err or "正股波动率数据不足"
    spot = float(bars.iloc[-1].close)
    rv = float(pd.to_numeric(bars.close, errors="coerce").pct_change().tail(20).std() * np.sqrt(252) * 100)
    expiries, err = client.option_expiration_dates(code)
    if err or expiries is None or expiries.empty:
        return base, err or "该正股没有可用港股期权到期日"
    valid = expiries[(expiries.option_expiry_date_distance >= 30)
                     & (expiries.option_expiry_date_distance <= 60)].sort_values("option_expiry_date_distance")
    candidates = []
    for _, expiry_row in valid.iterrows():
        expiry = str(expiry_row.strike_time)
        chain, chain_err = client.option_chain(code, expiry, expiry)
        if chain_err or chain is None or chain.empty:
            continue
        codes = chain.loc[chain.option_type.astype(str).str.upper() == "CALL", "code"].astype(str).tolist()
        snap, snap_err = client.market_snapshot(codes)
        if snap_err or snap is None or snap.empty:
            continue
        ranked = rank_contracts(snap, "CALL", rv)
        ivs = pd.to_numeric(snap.get("option_implied_volatility"), errors="coerce").dropna()
        base["term_structure"].append({"expiry": expiry,
                                       "days_to_expiry": int(expiry_row.option_expiry_date_distance),
                                       "median_iv_pct": round(float(ivs.median()), 2) if len(ivs) else None})
        for item in ranked[:3]:
            item.update({"expiry": expiry, "days_to_expiry": int(expiry_row.option_expiry_date_distance),
                         "max_loss_per_contract_hkd": item["ask"] * item["contract_size"]})
            candidates.append(item)
    base.update({"spot": spot, "realized_vol_20d_pct": rv, "candidates": candidates[:5]})
    if not candidates:
        base["reason"] = "没有合约同时通过Delta、IV/实现波动率、价差、成交量和持仓量门槛"
        return base, None
    best = candidates[0]
    assets = float((portfolio or {}).get("total_assets") or 0)
    cash = float((portfolio or {}).get("cash") or 0)
    budget = assets * .01
    risk_ok = assets > 0 and best["max_loss_per_contract_hkd"] <= min(cash, budget)
    base["gates"] = {"stock_direction": True, "contract_value": True,
                     "historical_robustness": bool(historical_gate_passed), "risk_budget": risk_ok,
                     "max_loss_budget_hkd": budget}
    base["contract"] = best
    if historical_gate_passed and risk_ok:
        base.update({"action": "REVIEW", "reason": "全部硬门控通过；仅生成下单前人工复核，不自动交易"})
    else:
        failed = [k for k, ok in base["gates"].items() if isinstance(ok, bool) and not ok]
        base["reason"] = "硬门控未通过：" + "、".join(failed)
    return base, None

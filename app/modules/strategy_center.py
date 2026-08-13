"""Read-only strategy centre for the two qualified daily strategies.

This module deliberately has no order-placement function.  It may read Futu
positions to make a signal position-aware, but all output is a proposed order.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from .orb_universe import HK_LIQUID_SEED

ROOT = Path(__file__).resolve().parents[2]
DAILY_DIR = ROOT / ".universal_daily_60"
UNIVERSE_FILE = ROOT / ".universal_daily" / "research_universe_60.csv"
CAPITAL = 20_000.0


def _serial(v: Any) -> Any:
    if isinstance(v, (np.bool_, bool)):
        return bool(v)
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.floating,)):
        return None if not np.isfinite(v) else float(v)
    return v


def _read_daily(path: Path) -> pd.DataFrame:
    x = pd.read_csv(path)
    x["time_key"] = pd.to_datetime(x["time_key"])
    return x.sort_values("time_key").drop_duplicates("time_key", keep="last").set_index("time_key")


def _ensure_universe(client) -> list[str]:
    """Create the production research universe from the existing liquid seed."""
    if UNIVERSE_FILE.exists():
        return []
    if not hasattr(client, "stock_basicinfo"):
        return ["初始化研究股票池失败: 客户端不支持基础资料查询"]
    try:
        frame, err = client.stock_basicinfo()
    except Exception as exc:  # noqa: BLE001
        return [f"初始化研究股票池失败: {exc}"]
    if err or frame is None or frame.empty:
        return [f"初始化研究股票池失败: {err or '无基础资料'}"]
    rows = frame[frame["code"].astype(str).isin(HK_LIQUID_SEED)].copy()
    if rows.empty:
        return ["初始化研究股票池失败: 未匹配到流动性股票"]
    name_col = "name" if "name" in rows.columns else "stock_name"
    lot_col = "lot_size" if "lot_size" in rows.columns else None
    out = pd.DataFrame({"code": rows["code"].astype(str),
                        "name": rows[name_col].astype(str),
                        "lot_size": rows[lot_col].astype(int) if lot_col else 100})
    UNIVERSE_FILE.parent.mkdir(exist_ok=True)
    out.to_csv(UNIVERSE_FILE, index=False)
    return []


def _positions(client) -> dict[str, float]:
    try:
        frame, err = client.positions()
        if err or frame is None or frame.empty:
            return {}
        return {str(r.code): float(getattr(r, "qty", 0) or 0) for _, r in frame.iterrows()}
    except Exception:  # noqa: BLE001 - status must degrade gracefully
        return {}


def _refresh_cache(client) -> list[str]:
    """Merge the latest adjusted daily bars into the research cache."""
    errors: list[str] = []
    errors.extend(_ensure_universe(client))
    if not UNIVERSE_FILE.exists():
        return errors
    codes = pd.read_csv(UNIVERSE_FILE)["code"].astype(str).tolist() + ["HK.800000"]
    DAILY_DIR.mkdir(exist_ok=True)
    for code in codes:
        frame, err = client.history_kline(code, max_count=260)
        if err or frame is None or frame.empty:
            errors.append(f"{code}: {err or '无日线数据'}")
            continue
        path = DAILY_DIR / f"{code.replace('.', '_')}.csv"
        new = frame.copy()
        if path.exists():
            new = pd.concat([pd.read_csv(path), new], ignore_index=True)
        new["time_key"] = pd.to_datetime(new["time_key"])
        new.sort_values("time_key").drop_duplicates("time_key", keep="last").to_csv(path, index=False)
    return errors


def _xiaomi_status(positions: dict[str, float]) -> dict:
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
        lot, budget = 200, CAPITAL * 0.50
        qty = int(budget // (latest.close * lot)) * lot
        action, reason = "BUY" if qty else "WAIT", "收盘价>MA60且MA20>MA60；下一交易日开盘执行"
    else:
        action, reason = "WAIT", "小米趋势条件尚未同时成立"
    qty = positions.get("HK.01810", 0) if action == "SELL" else int((CAPITAL * .5) // (latest.close * 200)) * 200
    return {
        "id": "xiaomi_trend_v1", "name": "小米专属趋势", "status": "已通过历史研究",
        "as_of": str(x.index[-1].date()), "action": action, "reason": reason,
        "price": float(latest.close), "ma20": float(ma20), "ma60": float(ma60),
        "held_qty": positions.get("HK.01810", 0), "suggested_qty": int(qty),
        "validation": {"return_pct": 28.9729, "max_drawdown_pct": -14.4996, "profit_factor": 1.9091},
    }


def _rotation_status(positions: dict[str, float]) -> dict:
    universe = pd.read_csv(UNIVERSE_FILE)
    lots = {str(r.code): int(r.lot_size) for _, r in universe.iterrows()}
    names = {str(r.code): str(r["name"]) for _, r in universe.iterrows()}
    idx = _read_daily(DAILY_DIR / "HK_800000.csv")
    latest_date = idx.index[-1]
    idx_ma120 = idx.close.rolling(120).mean().iloc[-1]
    market_ok = idx.close.iloc[-1] > idx_ma120
    candidates = []
    for path in DAILY_DIR.glob("HK_*.csv"):
        x = _read_daily(path)
        if len(x) < 121 or latest_date not in x.index:
            continue
        code = str(x.iloc[-1].get("code", ""))
        if code not in lots:
            continue
        close = x.close
        r = x.loc[latest_date]
        ma20, ma60, ma120 = (close.rolling(n).mean().loc[latest_date] for n in (20, 60, 120))
        turn20 = x.turnover.rolling(20).mean().loc[latest_date]
        vol60 = close.pct_change().rolling(60).std().loc[latest_date] * np.sqrt(252)
        mom = r.close / close.iloc[-121] - 1
        eligible = (market_ok and turn20 >= 100_000_000 and .12 <= vol60 <= .70
                    and r.close > ma60 and ma20 > ma60 > ma120 and mom >= .10)
        if not eligible:
            continue
        lot = lots[code]
        target = CAPITAL * .60 / 4
        qty = int(target // (r.close * lot)) * lot
        candidates.append({
            "code": code, "name": names.get(code, code), "price": float(r.close),
            "momentum_pct": float(mom * 100), "volatility_pct": float(vol60 * 100),
            "score": float(mom / vol60), "lot_size": lot, "suggested_qty": qty,
            "estimated_amount": float(qty * r.close), "affordable": qty > 0,
        })
    candidates.sort(key=lambda z: z["score"], reverse=True)
    dates = list(idx.index)
    review_dates = [d for i, d in enumerate(dates[:-1]) if i % 20 == 0]
    is_review = latest_date in review_dates
    held_codes = {c for c, q in positions.items() if q > 0}
    proposed = [c for c in candidates if c["affordable"]][:4]
    action = "REVIEW" if is_review and proposed else "WAIT"
    reason = ("正式调仓日：请核对建议订单" if action == "REVIEW"
              else "今天不是正式20交易日调仓点；候选仅供预览")
    return {
        "id": "hk_liquid_trend_rotation_v1", "name": "港股流动性趋势轮动", "status": "已通过历史研究",
        "as_of": str(latest_date.date()), "action": action, "reason": reason,
        "market": {"hsi_close": float(idx.close.iloc[-1]), "hsi_ma120": float(idx_ma120), "eligible": bool(market_ok)},
        "is_review_day": is_review, "current_strategy_holdings": sorted(held_codes & set(lots)),
        "candidates": candidates[:10], "proposed": proposed,
        "validation": {"return_pct": 47.1442, "max_drawdown_pct": -11.8561, "distinct_stocks": 12},
    }


def _breakout_status(positions: dict[str, float]) -> dict:
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
            lot=lots[code];qty=int((CAPITAL*.20)//(z.close*lot))*lot
            rows.append({"code":code,"name":names.get(code,code),"price":float(z.close),
                         "volume_ratio":float(vol_ratio),"prior_high120":float(prior_high),
                         "lot_size":lot,"suggested_qty":qty,"estimated_amount":float(qty*z.close)})
    rows.sort(key=lambda r:r["volume_ratio"],reverse=True)
    return {"id":"hk_long_term_high_breakout_v1","name":"港股长期新高突破","status":"已通过历史研究",
            "as_of":str(date.date()),"action":"BUY" if any(r["suggested_qty"] for r in rows[:4]) else "WAIT",
            "reason":"发现放量突破，建议下一交易日开盘核对" if rows else "恒指环境通过，但当前没有120日放量新高突破",
            "market_eligible":bool(market_ok),"candidates":rows[:4],
            "validation":{"return_pct":28.6405,"max_drawdown_pct":-12.5422,"profit_factor":1.7967,"trades":54}}


def get_status(client, refresh: bool = False) -> dict:
    required = [DAILY_DIR / "HK_01810.csv", DAILY_DIR / "HK_800000.csv"]
    errors = _refresh_cache(client) if refresh or any(not p.exists() for p in required) else []
    positions = _positions(client)
    def safe(fn, strategy_id, name):
        try:
            return fn(positions)
        except Exception as exc:  # noqa: BLE001
            return {"id": strategy_id, "name": name, "status": "数据不可用",
                    "action": "UNAVAILABLE", "reason": str(exc), "as_of": None,
                    "price": None, "ma20": None, "ma60": None, "suggested_qty": 0,
                    "market": {"hsi_close": None, "hsi_ma120": None, "eligible": False},
                    "market_eligible": False, "is_review_day": False,
                    "validation": {"return_pct": None, "max_drawdown_pct": None,
                                   "profit_factor": None, "distinct_stocks": 0, "trades": 0},
                    "candidates": []}
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "READ_ONLY_PAPER_ADVICE",
        "capital_hkd": CAPITAL,
        "refresh_errors": errors[:8],
        "strategies": [
            safe(_xiaomi_status, "xiaomi_trend_v1", "小米专属趋势"),
            safe(_rotation_status, "hk_liquid_trend_rotation_v1", "港股流动性趋势轮动"),
            safe(_breakout_status, "hk_long_term_high_breakout_v1", "港股长期新高突破"),
        ],
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

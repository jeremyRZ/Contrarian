"""Multi-underlying option discovery and quote tracking. No order submission."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sqlite3
import threading
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
DB_PATH = ROOT / ".runtime/option_workspace.sqlite3"
CACHE_PATH = ROOT / ".runtime/option_workspace.json"
SCAN_LOCK = threading.Lock()
LAST_ERROR = None


def number(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (ValueError, TypeError):
        return None


def rank_snapshot(snapshot, settings, now=None):
    now = now or datetime.now()
    minimum, maximum = int(settings.get("min_dte", 14)), int(settings.get("max_dte", 90))
    budget = number(settings.get("premium_budget"))
    rows = []
    for item in snapshot.to_dict("records"):
        side = str(item.get("option_type", ""))
        if side not in {"CALL", "PUT"}:
            continue
        try:
            expiry = str(item["strike_time"])[:10]
            dte = (datetime.fromisoformat(expiry).date() - now.date()).days
        except (KeyError, ValueError):
            continue
        if not minimum <= dte <= maximum:
            continue
        # Multiplier is monetary value per quoted option price, not stock board lot.
        multiplier = number(item.get("option_contract_multiplier"))
        if not multiplier or multiplier <= 0:
            continue
        bid, ask = number(item.get("bid_price")), number(item.get("ask_price"))
        last, strike = number(item.get("last_price")), number(item.get("option_strike_price"))
        if not strike or strike <= 0:
            continue
        two_sided = bool(bid and ask and 0 < bid <= ask)
        spread = (ask - bid) / ((ask + bid) / 2) * 100 if two_sided else None
        premium = ask * multiplier if ask and ask > 0 else None
        delta = number(item.get("option_delta"))
        volume, oi = number(item.get("volume")) or 0, number(item.get("option_open_interest")) or 0
        reasons = []
        if not two_sided:
            reasons.append("无双边报价")
        elif spread > float(settings.get("max_spread_pct", 20)):
            reasons.append("价差超过筛选值")
        if volume < 20 or oi < 50:
            reasons.append("成交或持仓不足")
        if delta is None or not .15 <= abs(delta) <= .75 or (side == "CALL" and delta < 0) or (side == "PUT" and delta > 0):
            reasons.append("Delta不在筛选区间")
        theta = number(item.get("option_theta"))
        rows.append({"code": str(item["code"]), "name": str(item.get("name", "")),
            "underlying": str(item.get("stock_owner", "")), "side": side,
            "expiry": expiry, "dte": dte, "strike": strike, "multiplier": multiplier,
            "bid": bid, "ask": ask, "last": last, "spread_pct": spread,
            "premium": premium, "last_trade_notional": last * multiplier if last and last > 0 else None,
            "within_budget": premium <= budget if premium is not None and budget is not None else None,
            "breakeven_at_expiry": (strike + ask if side == "CALL" else strike - ask) if premium is not None else None,
            "delta": delta, "iv_pct": number(item.get("option_implied_volatility")),
            "theta_per_contract": theta * multiplier if theta is not None else None,
            "volume": int(volume), "open_interest": int(oi),
            "last_trade_time": str(item.get("update_time", "")),
            "quote_available": two_sided, "liquidity_pass": not reasons, "reasons": reasons,
            "actionable": False})
    return sorted(rows, key=lambda x: (not x["liquidity_pass"], x["within_budget"] is False,
                                       x["spread_pct"] if x["spread_pct"] is not None else 10000,
                                       -x["volume"], x["code"]))


def connect():
    DB_PATH.parent.mkdir(exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.executescript("""
    CREATE TABLE IF NOT EXISTS scans(id TEXT PRIMARY KEY,market TEXT,payload TEXT);
    CREATE TABLE IF NOT EXISTS quotes(scan_id TEXT,code TEXT,bid REAL,ask REAL,payload TEXT,
      PRIMARY KEY(scan_id,code));
    """)
    return db


def cached():
    data = json.loads(CACHE_PATH.read_text(encoding="utf-8")) if CACHE_PATH.exists() else {
        "candidates": [], "underlyings": [], "generated_at": None}
    return {**data, "scanning": SCAN_LOCK.locked(), "scan_error": LAST_ERROR}


def refresh(client, config):
    global LAST_ERROR
    if not SCAN_LOCK.acquire(blocking=False):
        return cached()
    try:
        settings = config.get("option_workspace", {}) or {}
        market = str(settings.get("market", "HK")).upper()
        ranks, error = client.option_rankings(market)
        if error or not ranks:
            raise ValueError(error or "期权排行未返回")
        codes = ranks["contracts"]["code"].astype(str).tolist()
        if not codes:
            raise ValueError("期权排行为空")
        ranked_count = len(codes)
        # Keep following recent unexpired contracts when they leave the volume ranking.
        # ponytail: bounded to 500 tracked contracts; expose coverage instead of unbounded API calls.
        with closing(connect()) as db:
            tracked = db.execute("SELECT code,MAX(scan_id) FROM quotes GROUP BY code ORDER BY MAX(scan_id) DESC LIMIT 500").fetchall()
            for code, scan_id in tracked:
                row = json.loads(db.execute("SELECT payload FROM quotes WHERE code=? AND scan_id=?", (code, scan_id)).fetchone()[0])
                if row["expiry"] >= datetime.now().date().isoformat() and code not in codes:
                    codes.append(code)
        frames = []
        for offset in range(0, len(codes), 200):
            frame, error = client.market_snapshot(codes[offset:offset + 200])
            if error or frame is None or frame.empty:
                raise ValueError(error or "期权快照为空")
            frames.append(frame)
        snapshot = pd.concat(frames, ignore_index=True)
        candidates = rank_snapshot(snapshot, settings)
        tracking = rank_snapshot(snapshot, {**settings, "min_dte": 0, "max_dte": 3650})
        with closing(connect()) as db:
            for item in tracking:
                first = db.execute("SELECT scan_id,ask FROM quotes WHERE code=? AND bid>0 AND ask>=bid ORDER BY scan_id LIMIT 1", (item["code"],)).fetchone()
                item["first_two_sided_at"] = first[0] if first else None
                item["quote_return_pct"] = ((item["bid"] / first[1] - 1) * 100
                    if first and item["quote_available"] else None)
        tracked_by_code = {item["code"]: item for item in tracking}
        candidates = [tracked_by_code[item["code"]] for item in candidates]
        generated = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        # DataFrame JSON converts vendor NaN values into JSON null.
        underlyings = json.loads(ranks["underlyings"].to_json(orient="records", force_ascii=False))
        data = {"generated_at": generated, "market": market,
                "currency": "HKD" if market == "HK" else "USD",
                "source": "FUTU_OPTION_VOLUME_RANK", "underlying_total": ranks["underlying_total"],
                "contract_total": ranks["contract_total"], "ranked_contracts": ranked_count,
                "tracked_contracts": len(tracking), "tracking_limit": 500,
                "snapshots_received": len(snapshot), "candidates": candidates,
                "underlyings": underlyings, "settings": settings,
                "liquidity_pass_count": sum(c["liquidity_pass"] for c in candidates),
                "mode": "OPTION_RESEARCH", "orders_enabled": False,
                "cost_basis": "买入按卖一价×合约乘数；未含券商手续费。最近成交金额不是可成交报价。"}
        text = json.dumps(data, ensure_ascii=False, allow_nan=False)
        with closing(connect()) as db, db:
            db.execute("INSERT INTO scans VALUES(?,?,?)", (generated, market, text))
            for item in tracking:
                db.execute("INSERT INTO quotes VALUES(?,?,?,?,?)",
                           (generated, item["code"], item["bid"], item["ask"], json.dumps(item, ensure_ascii=False)))
        temp = CACHE_PATH.with_suffix(".new.json")
        temp.write_text(text, encoding="utf-8")
        temp.replace(CACHE_PATH)
        LAST_ERROR = None
        return data
    except Exception as exc:
        LAST_ERROR = str(exc)[:300]
        raise
    finally:
        SCAN_LOCK.release()


def digest(data):
    rows = data.get("candidates", [])
    lines = ["**Contrarian 期权候选摘要**",
             f"{data.get('market')}期权市场：{data.get('underlying_total')}个标的；检查成交量前{data.get('ranked_contracts')}份合约",
             f"快照：{data.get('generated_at')}"]
    selected, seen = [], set()
    for row in rows:
        key = (row["underlying"], row["side"])
        if key not in seen:
            selected.append(row); seen.add(key)
        if len(selected) == 5:
            break
    for row in selected:
        cost = (f"卖一金额{data['currency']} {row['premium']:,.0f}/张" if row["premium"] is not None else
                f"最近成交金额{data['currency']} {row['last_trade_notional']:,.0f}/张" if row["last_trade_notional"] else "无有效报价")
        lines.append(f"{row['name']}｜{row['code']}\n到期{row['expiry']}；{cost}；成交{row['volume']}张；" +
                     (f"价差{row['spread_pct']:.1f}%" if row["quote_available"] else "待双边报价"))
    if not selected:
        lines.append("本轮没有落入到期范围的合约。")
    lines.append("候选比较，不是已验证买入信号；详情见期权工作台。")
    return "\n".join(lines)

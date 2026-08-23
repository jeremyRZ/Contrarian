"""Append-only forward-validation signal ledger."""
from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime
from pathlib import Path

import pandas as pd

from .supertrend_research import SuperTrendParams, supertrend

ROOT = Path(__file__).resolve().parents[2]
DB_PATH = ROOT / ".runtime" / "forward_ledger.sqlite3"
DAILY_DIR = ROOT / ".universal_daily_60"


def _connect():
    DB_PATH.parent.mkdir(exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("""CREATE TABLE IF NOT EXISTS signals (
        id INTEGER PRIMARY KEY, recorded_at TEXT NOT NULL, signal_date TEXT NOT NULL,
        strategy_id TEXT NOT NULL, strategy_name TEXT NOT NULL, action TEXT NOT NULL,
        reason TEXT NOT NULL, payload TEXT NOT NULL,
        UNIQUE(signal_date, strategy_id, action))""")
    db.execute("""CREATE TABLE IF NOT EXISTS shadow_events (
        id INTEGER PRIMARY KEY, recorded_at TEXT NOT NULL, signal_date TEXT NOT NULL,
        strategy_id TEXT NOT NULL, code TEXT NOT NULL, direction TEXT NOT NULL,
        signal_close REAL NOT NULL, payload TEXT NOT NULL,
        UNIQUE(signal_date, strategy_id, direction))""")
    db.execute("""CREATE TABLE IF NOT EXISTS shadow_meta (
        strategy_id TEXT PRIMARY KEY, started_at TEXT NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS universe_snapshots (
        id INTEGER PRIMARY KEY, recorded_at TEXT NOT NULL, snapshot_date TEXT NOT NULL,
        code TEXT NOT NULL, payload TEXT NOT NULL, UNIQUE(snapshot_date,code))""")
    db.execute("""CREATE TABLE IF NOT EXISTS paper_orders (
        id INTEGER PRIMARY KEY, recorded_at TEXT NOT NULL, signal_date TEXT NOT NULL,
        strategy_id TEXT NOT NULL, code TEXT NOT NULL, action TEXT NOT NULL,
        current_qty INTEGER NOT NULL, target_qty INTEGER NOT NULL, difference_qty INTEGER NOT NULL,
        signal_price REAL, payload TEXT NOT NULL,
        UNIQUE(signal_date,strategy_id,code,action))""")
    return db


def record_universe_snapshot(universe: dict, snapshot_date: str | None = None) -> int:
    """Append the exact investable universe visible on a date; never backfill it."""
    now = datetime.now().isoformat(timespec="seconds")
    snapshot_date = snapshot_date or now[:10]
    count = 0
    with _connect() as db:
        for stock in universe.get("stocks", []):
            code = str(stock.get("code") or "")
            if not code:
                continue
            cur = db.execute(
                "INSERT OR IGNORE INTO universe_snapshots(recorded_at,snapshot_date,code,payload) VALUES(?,?,?,?)",
                (now, snapshot_date, code,
                 json.dumps(stock, ensure_ascii=False, separators=(",", ":"), allow_nan=False)))
            count += cur.rowcount
    return count


def managed_codes(strategy_id: str) -> set[str]:
    """Codes previously entered by the strategy's shadow book and not exited."""
    with _connect() as db:
        rows = db.execute(
            "SELECT code,action FROM paper_orders WHERE strategy_id=? ORDER BY signal_date,id",
            (strategy_id,)).fetchall()
    managed: set[str] = set()
    for row in rows:
        if row["action"] == "BUY":
            managed.add(row["code"])
        elif row["action"] == "SELL":
            managed.discard(row["code"])
    return managed


def record_rotation_shadow(strategy: dict) -> int:
    """Persist review-day target-vs-position orders for later executable evaluation."""
    if strategy.get("id") != "hk_liquid_trend_rotation_v2" or not strategy.get("is_review_day"):
        return 0
    now = datetime.now().isoformat(timespec="seconds")
    signal_date = strategy.get("as_of") or now[:10]
    prices = {str(x.get("code")): x.get("price") for x in strategy.get("candidates", [])}
    count = 0
    with _connect() as db:
        for order in strategy.get("orders", []):
            if order.get("action") not in {"BUY", "SELL"} or not order.get("difference_qty"):
                continue
            code = str(order.get("code") or "")
            cur = db.execute(
                """INSERT OR IGNORE INTO paper_orders(
                    recorded_at,signal_date,strategy_id,code,action,current_qty,target_qty,
                    difference_qty,signal_price,payload) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (now, signal_date, strategy["id"], code, order["action"],
                 int(order.get("current_qty") or 0), int(order.get("target_qty") or 0),
                 int(order.get("difference_qty") or 0), prices.get(code),
                 json.dumps(order, ensure_ascii=False, separators=(",", ":"))))
            count += cur.rowcount
    return count


def _order_cost(notional: float) -> float:
    return (max(3.0, notional * .0003) + 15.0 +
            notional * (.000027 + .0000015 + .0000565 + .000042) +
            math.ceil(notional * .001))


def _paper_dashboard() -> dict:
    with _connect() as db:
        snapshots = db.execute(
            "SELECT COUNT(DISTINCT snapshot_date),COUNT(*) FROM universe_snapshots").fetchone()
        rows = [dict(row) for row in db.execute(
            "SELECT signal_date,strategy_id,code,action,current_qty,target_qty,difference_qty,signal_price,payload FROM paper_orders ORDER BY signal_date DESC,id DESC")]
    for row in rows:
        row["details"] = json.loads(row.pop("payload"))
        row.update({"next_open": None, "fill_price": None, "estimated_fee_hkd": None,
                    "slippage_bps": 8.0, "return_20d_pct": None,
                    "opportunity_cost_20d_pct": None, "status": "WAITING_FILL"})
        path = DAILY_DIR / f"{row['code'].replace('.', '_')}.csv"
        if not path.exists():
            continue
        bars = pd.read_csv(path)
        bars["time_key"] = pd.to_datetime(bars["time_key"], format="mixed")
        future = bars[bars.time_key > pd.Timestamp(row["signal_date"])].sort_values("time_key")
        if future.empty:
            continue
        next_open = float(future.iloc[0].open)
        side = 1 if row["action"] == "BUY" else -1
        fill = next_open * (1 + side * .0008)
        qty = abs(int(row["difference_qty"]))
        row["next_open"] = next_open; row["fill_price"] = fill
        row["estimated_fee_hkd"] = round(_order_cost(fill * qty), 2)
        row["status"] = "FILLED_SHADOW"
        if len(future) >= 20:
            close20 = float(future.iloc[19].close)
            raw = (close20 / fill - 1) * 100
            row["return_20d_pct"] = round(raw if row["action"] == "BUY" else -raw, 2)
            row["opportunity_cost_20d_pct"] = round(-raw if row["action"] == "SELL" else 0.0, 2)
            row["status"] = "MATURE"
    return {"snapshot_days": int(snapshots[0] or 0), "snapshot_rows": int(snapshots[1] or 0),
            "orders": rows, "order_count": len(rows),
            "note": "影子成交采用下一交易日开盘加减8bps滑点，并计入港股小额订单费用。"}


def record_supertrend_exit_shadow(bars: pd.DataFrame | None = None) -> int:
    """Record a new Xiaomi/SuperTrend disagreement event, never an order."""
    strategy_id = "xiaomi_supertrend_exit_shadow_v1"
    now = datetime.now().isoformat(timespec="seconds")
    with _connect() as db:
        db.execute("INSERT OR IGNORE INTO shadow_meta(strategy_id,started_at) VALUES(?,?)",
                   (strategy_id, now))
    if bars is None:
        path = DAILY_DIR / "HK_01810.csv"
        if not path.exists():
            return 0
        bars = pd.read_csv(path)
    frame = bars.copy().sort_values("time_key").drop_duplicates("time_key", keep="last")
    if len(frame) < 35:
        return 0
    frame["time_key"] = pd.to_datetime(frame["time_key"], format="mixed")
    close = pd.to_numeric(frame["close"], errors="coerce")
    base = pd.Series(0, index=frame.index, dtype=int)
    momentum = close.pct_change(20)
    base.loc[momentum > .05] = 1
    base.loc[momentum < -.05] = -1
    trend = supertrend(frame, SuperTrendParams(14, 3.5))["st_direction"]
    conflict = (base != 0) & (trend == -base)
    if not bool(conflict.iloc[-1]) or bool(conflict.iloc[-2]):
        return 0
    latest = frame.iloc[-1]
    direction = "EXIT_LONG" if int(base.iloc[-1]) == 1 else "EXIT_SHORT"
    payload = {
        "research_only": True, "notification": False, "order": False,
        "base": "20日动量阈值正负5%", "supertrend": {"atr_period": 14, "multiplier": 3.5},
        "momentum_20d_pct": float(momentum.iloc[-1] * 100),
        "supertrend_direction": int(trend.iloc[-1]),
    }
    with _connect() as db:
        cur = db.execute(
            "INSERT OR IGNORE INTO shadow_events(recorded_at,signal_date,strategy_id,code,direction,signal_close,payload) VALUES(?,?,?,?,?,?,?)",
            (now, str(latest.time_key.date()), strategy_id, "HK.01810", direction,
             float(latest.close), json.dumps(payload, ensure_ascii=False, separators=(",", ":"))))
        return cur.rowcount


def _shadow_dashboard() -> dict:
    strategy_id = "xiaomi_supertrend_exit_shadow_v1"
    with _connect() as db:
        meta = db.execute("SELECT started_at FROM shadow_meta WHERE strategy_id=?",
                          (strategy_id,)).fetchone()
        rows = [dict(row) for row in db.execute(
            "SELECT recorded_at,signal_date,direction,signal_close,payload FROM shadow_events WHERE strategy_id=? ORDER BY signal_date DESC,id DESC",
            (strategy_id,))]
    started_at = meta["started_at"] if meta else None
    elapsed_days = ((datetime.now() - datetime.fromisoformat(started_at)).days
                    if started_at else 0)
    path = DAILY_DIR / "HK_01810.csv"
    bars = None
    if path.exists():
        try:
            bars = pd.read_csv(path)
            bars["time_key"] = pd.to_datetime(bars["time_key"], format="mixed")
            bars = bars.sort_values("time_key")
        except Exception:  # noqa: BLE001
            bars = None
    for row in rows:
        row["details"] = json.loads(row.pop("payload"))
        row["next_open"] = None
        row["returns"] = {}
        if bars is None:
            continue
        future = bars[bars.time_key > pd.Timestamp(row["signal_date"])]
        if future.empty:
            continue
        entry = float(future.iloc[0].open)
        row["next_open"] = entry
        side = -1 if row["direction"] == "EXIT_LONG" else 1
        for horizon in (5, 10, 20):
            if len(future) >= horizon:
                hold_return = float(future.iloc[horizon - 1].close) / entry - 1
                row["returns"][str(horizon)] = round(side * hold_return * 100, 2)
    mature = [row["returns"]["20"] for row in rows if "20" in row["returns"]]
    return {
        "strategy_id": strategy_id, "name": "小米 SuperTrend 退出影子记录",
        "status": "READY_FOR_REVIEW" if len(rows) >= 30 or elapsed_days >= 183 else "COLLECTING",
        "started_at": started_at, "elapsed_days": elapsed_days, "event_count": len(rows),
        "target_events": 30, "target_days": 183,
        "mature_20d_count": len(mature),
        "average_saved_return_20d_pct": round(sum(mature) / len(mature), 2) if mature else None,
        "events": rows,
        "note": "正数表示影子退出优于继续持有；仅研究记录，不产生通知或订单。",
    }


def record_status(status: dict) -> int:
    now = datetime.now().isoformat(timespec="seconds")
    count = 0
    with _connect() as db:
        for item in status.get("strategies", []):
            signal_date = item.get("as_of") or now[:10]
            cur = db.execute(
                "INSERT OR IGNORE INTO signals(recorded_at,signal_date,strategy_id,strategy_name,action,reason,payload) VALUES(?,?,?,?,?,?,?)",
                (now, signal_date, item.get("id", "unknown"), item.get("name", "未知策略"),
                 item.get("action", "UNAVAILABLE"), item.get("reason", ""),
                 json.dumps(item, ensure_ascii=False, separators=(",", ":"))))
            count += cur.rowcount
    return count


def dashboard(limit: int = 200) -> dict:
    with _connect() as db:
        rows = [dict(r) for r in db.execute(
            "SELECT recorded_at,signal_date,strategy_id,strategy_name,action,reason,payload FROM signals ORDER BY signal_date DESC,id DESC LIMIT ?", (limit,))]
    for row in rows:
        row["details"] = json.loads(row.pop("payload"))
    evaluations = []
    by_strategy = {}
    for row in rows:
        if row["action"] not in {"BUY", "REVIEW"}:
            continue
        details = row["details"] or {}
        candidates = details.get("proposed") or details.get("candidates") or []
        for candidate in candidates:
            code, entry = candidate.get("code"), candidate.get("price")
            path = DAILY_DIR / f"{str(code).replace('.', '_')}.csv"
            if not code or not entry or not path.exists():
                continue
            try:
                bars = pd.read_csv(path)
                bars["time_key"] = pd.to_datetime(bars["time_key"], format="mixed")
                future = bars[bars["time_key"] > pd.Timestamp(row["signal_date"])].sort_values("time_key")
            except Exception:  # noqa: BLE001
                continue
            result = {"strategy_id": row["strategy_id"], "strategy_name": row["strategy_name"],
                      "signal_date": row["signal_date"], "code": code, "entry_price": float(entry),
                      "returns": {}}
            for horizon in (5, 20):
                if len(future) >= horizon:
                    close = float(future.iloc[horizon - 1]["close"])
                    result["returns"][str(horizon)] = round((close / float(entry) - 1) * 100, 2)
            if result["returns"]:
                evaluations.append(result)
                by_strategy.setdefault(row["strategy_id"], {"name": row["strategy_name"], "returns_20d": [], "returns_5d": []})
                for horizon in (5, 20):
                    value = result["returns"].get(str(horizon))
                    if value is not None:
                        by_strategy[row["strategy_id"]][f"returns_{horizon}d"].append(value)
    strategy_stats = []
    for strategy_id, values in by_strategy.items():
        mature = values["returns_20d"] or values["returns_5d"]
        count = len(mature)
        avg = round(sum(mature) / count, 2) if count else None
        win_rate = round(sum(x > 0 for x in mature) / count * 100, 1) if count else None
        status = "REVIEW_REQUIRED" if count >= 5 and (avg or 0) <= 0 else ("ACTIVE" if count >= 5 else "COLLECTING")
        strategy_stats.append({"strategy_id": strategy_id, "strategy_name": values["name"],
                               "mature_samples": count, "average_return_pct": avg,
                               "win_rate_pct": win_rate, "status": status})
    actionable = sum(r["action"] in {"BUY", "SELL", "REVIEW"} for r in rows)
    unavailable = sum(r["action"] == "UNAVAILABLE" for r in rows)
    mature_returns = [x["returns"].get("20") for x in evaluations if x["returns"].get("20") is not None]
    if not mature_returns:
        mature_returns = [x["returns"].get("5") for x in evaluations if x["returns"].get("5") is not None]
    avg_return = round(sum(mature_returns) / len(mature_returns), 2) if mature_returns else None
    return {"records": rows, "summary": {"total": len(rows), "actionable": actionable,
            "unavailable": unavailable, "trades": len(mature_returns), "return_pct": avg_return,
            "profit_factor": None, "max_drawdown_pct": None},
            "evaluations": evaluations, "strategy_stats": strategy_stats,
            "shadow": _shadow_dashboard(),
            "paper_execution": _paper_dashboard(),
            "note": ("收益为信号后5/20交易日收盘的前向观察值，不等同真实成交收益。"
                     if evaluations else "尚无到期样本；系统会在信号后第5和第20个交易日自动评价。")}

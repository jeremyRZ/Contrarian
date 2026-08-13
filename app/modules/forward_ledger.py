"""Append-only forward-validation signal ledger."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DB_PATH = ROOT / ".runtime" / "forward_ledger.sqlite3"


def _connect():
    DB_PATH.parent.mkdir(exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("""CREATE TABLE IF NOT EXISTS signals (
        id INTEGER PRIMARY KEY, recorded_at TEXT NOT NULL, signal_date TEXT NOT NULL,
        strategy_id TEXT NOT NULL, strategy_name TEXT NOT NULL, action TEXT NOT NULL,
        reason TEXT NOT NULL, payload TEXT NOT NULL,
        UNIQUE(signal_date, strategy_id, action))""")
    return db


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
    actionable = sum(r["action"] in {"BUY", "SELL", "REVIEW"} for r in rows)
    unavailable = sum(r["action"] == "UNAVAILABLE" for r in rows)
    return {"records": rows, "summary": {"total": len(rows), "actionable": actionable,
            "unavailable": unavailable, "trades": 0, "return_pct": None,
            "profit_factor": None, "max_drawdown_pct": None},
            "note": "绩效指标将在下一交易日开盘模拟成交后开始生成。"}

"""Append-only audit trail for notification attempts; never stores webhooks."""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DB_PATH = ROOT / ".runtime" / "notification_ledger.sqlite3"


def _connect():
    DB_PATH.parent.mkdir(exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("""CREATE TABLE IF NOT EXISTS notification_events (
        id INTEGER PRIMARY KEY, attempted_at TEXT NOT NULL,
        fingerprint TEXT NOT NULL, category TEXT NOT NULL,
        status TEXT NOT NULL, channel TEXT NOT NULL,
        message TEXT NOT NULL, detail TEXT NOT NULL DEFAULT ''
    )""")
    return db


def category_for(fingerprint: str) -> str:
    prefix = str(fingerprint).split(":", 1)[0].lower()
    return {
        "xiaomi-stock": "XIAOMI_STOCK",
        "xiaomi-directional": "XIAOMI_STOCK",
        "xiaomi-option": "XIAOMI_OPTION",
        "alert": "POSITION_RISK",
        "price": "PRICE_ALERT",
        "intraday": "INTRADAY_SIGNAL",
        "screener": "BUY_SCAN",
        "rebalance": "REBALANCE_REVIEW",
    }.get(prefix, prefix.upper() or "GENERAL")


def record(fingerprint: str, message: str, status: str, *, detail: str = "") -> int:
    with _connect() as db:
        cur = db.execute(
            "INSERT INTO notification_events(attempted_at,fingerprint,category,status,channel,message,detail) VALUES(?,?,?,?,?,?,?)",
            (datetime.now().isoformat(timespec="seconds"), str(fingerprint),
             category_for(fingerprint), str(status), "WECOM", str(message), str(detail)[:500]),
        )
        return int(cur.lastrowid)


def dashboard(limit: int = 200) -> dict:
    with _connect() as db:
        rows = [dict(row) for row in db.execute(
            "SELECT id,attempted_at,fingerprint,category,status,channel,message,detail "
            "FROM notification_events ORDER BY id DESC LIMIT ?", (int(limit),))]
        counts = {row["status"]: row["n"] for row in db.execute(
            "SELECT status,COUNT(*) AS n FROM notification_events GROUP BY status")}
    return {"records": rows, "summary": {"total": sum(counts.values()), "by_status": counts},
            "note": "仅记录提醒内容与发送结果，不保存企业微信Webhook或账户凭证。"}

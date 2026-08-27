"""Append-only audit trail for notification attempts; never stores webhooks."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
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
    db.execute("""CREATE TABLE IF NOT EXISTS notification_outbox (
        id INTEGER PRIMARY KEY, fingerprint TEXT NOT NULL UNIQUE,
        message TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'PENDING',
        attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at TEXT NOT NULL,
        last_error TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
        delivered_at TEXT
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
        "rebalance": "REBALANCE_REVIEW",
        "production": "PRODUCTION_SIGNAL",
        "execution": "EXECUTION_REMINDER",
    }.get(prefix, prefix.upper() or "GENERAL")


def record(fingerprint: str, message: str, status: str, *, detail: str = "",
           channel: str = "WECOM") -> int:
    with _connect() as db:
        cur = db.execute(
            "INSERT INTO notification_events(attempted_at,fingerprint,category,status,channel,message,detail) VALUES(?,?,?,?,?,?,?)",
            (datetime.now().isoformat(timespec="seconds"), str(fingerprint),
             category_for(fingerprint), str(status), str(channel), str(message), str(detail)[:500]),
        )
        return int(cur.lastrowid)


def was_sent_recently(fingerprint: str, seconds: int, *, channel: str | None = None) -> bool:
    with _connect() as db:
        if channel:
            row = db.execute(
                "SELECT attempted_at FROM notification_events "
                "WHERE fingerprint=? AND status='SENT' AND channel=? ORDER BY id DESC LIMIT 1",
                (str(fingerprint), str(channel)),
            ).fetchone()
        else:
            row = db.execute(
                "SELECT attempted_at FROM notification_events "
                "WHERE fingerprint=? AND status='SENT' ORDER BY id DESC LIMIT 1",
                (str(fingerprint),),
            ).fetchone()
    if not row:
        return False
    try:
        age = (datetime.now() - datetime.fromisoformat(row["attempted_at"])).total_seconds()
        return age < max(0, int(seconds))
    except (TypeError, ValueError):
        return False


def enqueue(fingerprint: str, message: str, error: str = "") -> int:
    now = datetime.now().isoformat(timespec="seconds")
    with _connect() as db:
        db.execute("""INSERT INTO notification_outbox(
            fingerprint,message,status,attempts,next_attempt_at,last_error,created_at)
            VALUES(?,?,'PENDING',0,?,?,?)
            ON CONFLICT(fingerprint) DO UPDATE SET
              message=excluded.message,status='PENDING',last_error=excluded.last_error,
              next_attempt_at=excluded.next_attempt_at
            WHERE notification_outbox.status!='DELIVERED'""",
            (str(fingerprint), str(message), now, str(error)[:500], now))
        row = db.execute("SELECT id FROM notification_outbox WHERE fingerprint=?",
                         (str(fingerprint),)).fetchone()
        return int(row["id"])


def due(limit: int = 20) -> list[dict]:
    now = datetime.now().isoformat(timespec="seconds")
    with _connect() as db:
        return [dict(row) for row in db.execute(
            "SELECT id,fingerprint,message,attempts FROM notification_outbox "
            "WHERE status='PENDING' AND next_attempt_at<=? ORDER BY id LIMIT ?",
            (now, int(limit)))]


def mark_delivered(item_id: int) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    with _connect() as db:
        db.execute("UPDATE notification_outbox SET status='DELIVERED',delivered_at=? WHERE id=?",
                   (now, int(item_id)))


def mark_failed(item_id: int, attempts: int, error: str) -> None:
    attempts = int(attempts) + 1
    delay = min(3600, 30 * (2 ** min(attempts - 1, 7)))
    next_attempt = (datetime.now() + timedelta(seconds=delay)).isoformat(timespec="seconds")
    with _connect() as db:
        db.execute("UPDATE notification_outbox SET attempts=?,next_attempt_at=?,last_error=? WHERE id=?",
                   (attempts, next_attempt, str(error)[:500], int(item_id)))


def dashboard(limit: int = 200) -> dict:
    with _connect() as db:
        rows = [dict(row) for row in db.execute(
            "SELECT id,attempted_at,fingerprint,category,status,channel,message,detail "
            "FROM notification_events ORDER BY id DESC LIMIT ?", (int(limit),))]
        counts = {row["status"]: row["n"] for row in db.execute(
            "SELECT status,COUNT(*) AS n FROM notification_events GROUP BY status")}
        outbox = {row["status"]: row["n"] for row in db.execute(
            "SELECT status,COUNT(*) AS n FROM notification_outbox GROUP BY status")}
    return {"records": rows, "summary": {"total": sum(counts.values()), "by_status": counts,
            "outbox": outbox},
            "note": "仅记录提醒内容与发送结果，不保存企业微信Webhook或账户凭证。"}

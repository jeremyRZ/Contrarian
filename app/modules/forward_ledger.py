"""Append-only forward-validation signal ledger."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

import pandas as pd

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
            "note": ("收益为信号后5/20交易日收盘的前向观察值，不等同真实成交收益。"
                     if evaluations else "尚无到期样本；系统会在信号后第5和第20个交易日自动评价。")}

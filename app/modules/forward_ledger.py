"""Append-only forward-validation signal ledger."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

import pandas as pd

from app.hk_costs import SLIPPAGE_BPS, affordable_board_lot, order_cost
from .supertrend_research import SuperTrendParams, supertrend

ROOT = Path(__file__).resolve().parents[2]
DB_PATH = ROOT / ".runtime" / "forward_ledger.sqlite3"
DAILY_DIR = ROOT / ".universal_daily_60"
SHADOW_CAPITAL_HKD = 20_000.0


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
    db.execute("""CREATE TABLE IF NOT EXISTS paper_fills (
        id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL UNIQUE,
        filled_at TEXT NOT NULL, fill_date TEXT NOT NULL, next_open REAL NOT NULL,
        fill_price REAL NOT NULL, fee_hkd REAL NOT NULL, slippage_bps REAL NOT NULL,
        FOREIGN KEY(order_id) REFERENCES paper_orders(id))""")
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
            """SELECT o.code,o.action FROM paper_orders o
            JOIN paper_fills f ON f.order_id=o.id
            WHERE o.strategy_id=? ORDER BY o.signal_date,o.id""",
            (strategy_id,)).fetchall()
    managed: set[str] = set()
    for row in rows:
        if row["action"] == "BUY":
            managed.add(row["code"])
        elif row["action"] == "SELL":
            managed.discard(row["code"])
    return managed


def record_rotation_shadow(strategy: dict) -> int:
    """Persist a fixed-HK$20k paper rebalance, independent of the live account."""
    if strategy.get("id") != "hk_liquid_trend_rotation_v2" or not strategy.get("is_review_day"):
        return 0
    now = datetime.now().isoformat(timespec="seconds")
    signal_date = strategy.get("as_of") or now[:10]
    targets = {str(x.get("code")): x for x in strategy.get("shadow_targets", [])[:4]}
    per_position_budget = SHADOW_CAPITAL_HKD / 4
    count = 0
    with _connect() as db:
        position_rows = db.execute(
            """SELECT o.code,o.action,o.difference_qty FROM paper_orders o
            JOIN paper_fills f ON f.order_id=o.id
            WHERE o.strategy_id=? ORDER BY o.signal_date,o.id""",
            (strategy["id"],)).fetchall()
        current: dict[str, int] = {}
        for row in position_rows:
            side = 1 if row["action"] == "BUY" else -1
            current[row["code"]] = current.get(row["code"], 0) + side * abs(int(row["difference_qty"]))
        desired = {
            code: affordable_board_lot(float(item.get("price") or 0), per_position_budget,
                                       int(item.get("lot_size") or 100))
            for code, item in targets.items()
        }
        for code in sorted(set(current) | set(desired)):
            difference = desired.get(code, 0) - max(current.get(code, 0), 0)
            if not difference:
                continue
            action = "BUY" if difference > 0 else "SELL"
            target = desired.get(code, 0)
            item = targets.get(code, {})
            payload = {"source": "FIXED_CAPITAL_SHADOW", "paper_only": True,
                       "shadow_capital_hkd": SHADOW_CAPITAL_HKD,
                       "name": item.get("name"), "reason": strategy.get("reason", "")}
            cur = db.execute(
                """INSERT OR IGNORE INTO paper_orders(
                    recorded_at,signal_date,strategy_id,code,action,current_qty,target_qty,
                    difference_qty,signal_price,payload) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (now, signal_date, strategy["id"], code, action,
                 max(current.get(code, 0), 0), target, difference,
                 item.get("price"), json.dumps(payload, ensure_ascii=False, separators=(",", ":"))))
            count += cur.rowcount
    return count


def record_xiaomi_shadow(strategy: dict) -> int:
    """Record one independent Xiaomi paper position, never the real holding."""
    if strategy.get("id") != "xiaomi_trend_v1":
        return 0
    action = strategy.get("action")
    if action not in {"BUY", "SELL"}:
        return 0
    code = "HK.01810"
    now = datetime.now().isoformat(timespec="seconds")
    signal_date = strategy.get("as_of") or now[:10]
    with _connect() as db:
        rows = db.execute(
            """SELECT o.action,o.difference_qty FROM paper_orders o
            JOIN paper_fills f ON f.order_id=o.id
            WHERE o.strategy_id=? AND o.code=?""",
            (strategy["id"], code)).fetchall()
        current = sum(abs(int(row["difference_qty"])) * (1 if row["action"] == "BUY" else -1)
                      for row in rows)
        if action == "BUY":
            if current > 0:
                return 0
            difference = affordable_board_lot(float(strategy.get("price") or 0),
                                              SHADOW_CAPITAL_HKD * .5, 200)
            target = difference
        else:
            if current <= 0:
                return 0
            difference = -current
            target = 0
        if not difference:
            return 0
        payload = {"source": "FIXED_CAPITAL_SHADOW", "paper_only": True,
                   "shadow_capital_hkd": SHADOW_CAPITAL_HKD,
                   "recommendation_qty": int(strategy.get("suggested_qty") or 0),
                   "reason": strategy.get("reason", "")}
        cur = db.execute(
            """INSERT OR IGNORE INTO paper_orders(
                recorded_at,signal_date,strategy_id,code,action,current_qty,target_qty,
                difference_qty,signal_price,payload) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (now, signal_date, strategy["id"], code, action, current, target, difference,
             strategy.get("price"), json.dumps(payload, ensure_ascii=False, separators=(",", ":"))))
        return cur.rowcount


def settle_paper_orders() -> int:
    """Write each next-open paper fill once; later CSV changes cannot rewrite history."""
    now = datetime.now().isoformat(timespec="seconds")
    count = 0
    with _connect() as db:
        pending = db.execute("""SELECT o.id,o.signal_date,o.code,o.action,o.difference_qty
            FROM paper_orders o LEFT JOIN paper_fills f ON f.order_id=o.id
            WHERE f.order_id IS NULL ORDER BY o.signal_date,o.id""").fetchall()
        for order in pending:
            path = DAILY_DIR / f"{order['code'].replace('.', '_')}.csv"
            if not path.exists():
                continue
            bars = pd.read_csv(path)
            bars["time_key"] = pd.to_datetime(bars["time_key"], format="mixed")
            future = bars[bars.time_key > pd.Timestamp(order["signal_date"])].sort_values("time_key")
            if future.empty:
                continue
            first = future.iloc[0]
            next_open = float(first.open)
            side = 1 if order["action"] == "BUY" else -1
            fill = next_open * (1 + side * SLIPPAGE_BPS / 10_000)
            qty = abs(int(order["difference_qty"]))
            cur = db.execute("""INSERT OR IGNORE INTO paper_fills(
                order_id,filled_at,fill_date,next_open,fill_price,fee_hkd,slippage_bps)
                VALUES(?,?,?,?,?,?,?)""", (order["id"], now, str(first.time_key.date()), next_open,
                fill, round(order_cost(fill * qty, include_slippage=False), 2), SLIPPAGE_BPS))
            count += cur.rowcount
    return count


def _paper_metrics(rows: list[dict]) -> list[dict]:
    books: dict[tuple[str, str], dict] = {}
    stats: dict[str, dict] = {}
    for row in sorted(rows, key=lambda item: (item["signal_date"], item["action"] != "BUY")):
        if row["status"] not in {"FILLED_SHADOW", "MATURE"}:
            continue
        strategy_id, code = row["strategy_id"], row["code"]
        book = books.setdefault((strategy_id, code), {"qty": 0, "basis": 0.0})
        stat = stats.setdefault(strategy_id, {"strategy_id": strategy_id,
            "complete_round_trips": 0, "realized_pnl_hkd": 0.0,
            "gross_profit_hkd": 0.0, "gross_loss_hkd": 0.0,
            "max_drawdown_pct": 0.0, "peak_equity_hkd": 20_000.0})
        qty = abs(int(row["difference_qty"])); value = float(row["fill_price"]) * qty
        fee = float(row["estimated_fee_hkd"] or 0)
        if row["action"] == "BUY":
            book["qty"] += qty
            book["basis"] += value + fee
            continue
        sold = min(qty, int(book["qty"]))
        if sold <= 0:
            continue
        basis = book["basis"] * sold / book["qty"]
        pnl = float(row["fill_price"]) * sold - fee * sold / qty - basis
        book["qty"] -= sold; book["basis"] -= basis
        stat["realized_pnl_hkd"] += pnl
        stat["gross_profit_hkd"] += max(pnl, 0)
        stat["gross_loss_hkd"] += min(pnl, 0)
        equity = 20_000 + stat["realized_pnl_hkd"]
        stat["peak_equity_hkd"] = max(stat["peak_equity_hkd"], equity)
        drawdown = (equity / stat["peak_equity_hkd"] - 1) * 100
        stat["max_drawdown_pct"] = min(stat["max_drawdown_pct"], drawdown)
        if book["qty"] == 0:
            stat["complete_round_trips"] += 1
    result = []
    for stat in stats.values():
        loss = abs(stat.pop("gross_loss_hkd")); profit = stat.pop("gross_profit_hkd")
        stat.pop("peak_equity_hkd")
        stat["realized_pnl_hkd"] = round(stat["realized_pnl_hkd"], 2)
        stat["net_return_pct"] = round(stat["realized_pnl_hkd"] / 20_000 * 100, 2)
        stat["profit_factor"] = round(profit / loss, 3) if loss else None
        stat["max_drawdown_pct"] = round(stat["max_drawdown_pct"], 2)
        stat["modeled_fill_deviation_bps"] = SLIPPAGE_BPS
        result.append(stat)
    return result


def _paper_dashboard() -> dict:
    with _connect() as db:
        snapshots = db.execute(
            "SELECT COUNT(DISTINCT snapshot_date),COUNT(*) FROM universe_snapshots").fetchone()
        rows = [dict(row) for row in db.execute("""SELECT o.signal_date,o.strategy_id,o.code,
            o.action,o.current_qty,o.target_qty,o.difference_qty,o.signal_price,o.payload,
            f.fill_date,f.next_open,f.fill_price,f.fee_hkd AS estimated_fee_hkd,
            f.slippage_bps FROM paper_orders o LEFT JOIN paper_fills f ON f.order_id=o.id
            ORDER BY o.signal_date DESC,o.id DESC""")]
        snapshot_dates = {row[0] for row in db.execute(
            "SELECT DISTINCT snapshot_date FROM universe_snapshots")}
        coverage = db.execute(
            "SELECT MIN(snapshot_date),MAX(snapshot_date) FROM universe_snapshots").fetchone()
    for row in rows:
        row["details"] = json.loads(row.pop("payload"))
        row.update({"return_20d_pct": None,
                    "opportunity_cost_20d_pct": None, "status": "WAITING_FILL"})
        if row["fill_price"] is None:
            continue
        row["status"] = "FILLED_SHADOW"
        path = DAILY_DIR / f"{row['code'].replace('.', '_')}.csv"
        if not path.exists():
            continue
        bars = pd.read_csv(path)
        bars["time_key"] = pd.to_datetime(bars["time_key"], format="mixed")
        future = bars[bars.time_key > pd.Timestamp(row["signal_date"])].sort_values("time_key")
        if len(future) >= 20:
            close20 = float(future.iloc[19].close)
            raw = (close20 / float(row["fill_price"]) - 1) * 100
            row["return_20d_pct"] = round(raw if row["action"] == "BUY" else -raw, 2)
            row["opportunity_cost_20d_pct"] = round(-raw if row["action"] == "SELL" else 0.0, 2)
            row["status"] = "MATURE"
    metrics = _paper_metrics(rows)
    rotation_rows = [row for row in rows if row["strategy_id"] == "hk_liquid_trend_rotation_v2"]
    rotation_metric = next((item for item in metrics
                            if item["strategy_id"] == "hk_liquid_trend_rotation_v2"), {})
    review_points = len({row["signal_date"] for row in rotation_rows})
    completed_round_trips = int(rotation_metric.get("complete_round_trips") or 0)
    thresholds = {"minimum_review_points": 3, "minimum_complete_round_trips": 20,
                  "require_positive_net_return": True, "minimum_profit_factor": 1.2,
                  "maximum_drawdown_pct": -20, "maximum_fill_deviation_bps": 25,
                  "require_point_in_time_universe": True}
    blockers = []
    if review_points < thresholds["minimum_review_points"]:
        blockers.append(f"正式检查点{review_points}/3")
    if completed_round_trips < thresholds["minimum_complete_round_trips"]:
        blockers.append(f"完整模拟交易{completed_round_trips}/20")
    if completed_round_trips and (rotation_metric.get("net_return_pct") or 0) <= 0:
        blockers.append("扣费后收益未转正")
    if completed_round_trips and (rotation_metric.get("profit_factor") or 0) < 1.2:
        blockers.append("盈利因子低于1.2")
    if (rotation_metric.get("max_drawdown_pct") or 0) < -20:
        blockers.append("最大回撤超过20%")
    if any(row["signal_date"] not in snapshot_dates for row in rotation_rows):
        blockers.append("检查点缺少当日股票池快照")
    eligible = not blockers
    promotion = {"eligible": eligible, "status": "READY_FOR_REVIEW" if eligible else "COLLECTING",
                 "thresholds": thresholds,
                 "review_points": review_points,
                 "complete_round_trips": completed_round_trips, "blockers": blockers,
                 "note": "只做资格报告，不自动升级策略，也不允许按近期结果临时调参。"}
    return {"snapshot_days": int(snapshots[0] or 0), "snapshot_rows": int(snapshots[1] or 0),
            "point_in_time_coverage": {"first_date": coverage[0], "last_date": coverage[1],
                                       "days": int(snapshots[0] or 0)},
            "shadow_capital_hkd": SHADOW_CAPITAL_HKD,
            "orders": rows, "order_count": len(rows), "strategy_metrics": metrics,
            "promotion": promotion,
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

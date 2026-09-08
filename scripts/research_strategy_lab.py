"""Offline fixed-rule comparisons and append-only point-in-time research ledger."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from contextlib import closing
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.hk_costs import MODEL_ID, order_cost

FAMILIES = ("momentum120", "momentum60", "breakout55", "pullback2", "lowvol_control")
CAPITALS = ((5790.32, 1), (20000.0, 2))
VERSION = "fixed_rules_v1"


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def connect(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.executescript("""
    CREATE TABLE IF NOT EXISTS universe(date TEXT,code TEXT,payload TEXT,
      PRIMARY KEY(date,code));
    CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY,recorded_at TEXT,version TEXT,
      manifest TEXT,report TEXT);
    CREATE TABLE IF NOT EXISTS candidates(version TEXT,date TEXT,family TEXT,code TEXT,
      rank INTEGER,score REAL,eligible INTEGER,reason TEXT,price REAL,lot INTEGER,
      affordable_small INTEGER,PRIMARY KEY(version,date,family,code));
    CREATE TABLE IF NOT EXISTS outcomes(version TEXT,date TEXT,family TEXT,code TEXT,
      horizon INTEGER,entry_date TEXT,exit_date TEXT,net_return_pct REAL,
      PRIMARY KEY(version,date,family,code,horizon));
    CREATE TABLE IF NOT EXISTS simulated_fills(version TEXT,capital REAL,family TEXT,
      date TEXT,code TEXT,side TEXT,qty INTEGER,price REAL,fee REAL,
      PRIMARY KEY(version,capital,family,date,code,side));
    CREATE TABLE IF NOT EXISTS simulated_equity(version TEXT,capital REAL,family TEXT,
      date TEXT,equity REAL,cash REAL,PRIMARY KEY(version,capital,family,date));
    """)
    columns = {r[1] for r in db.execute("PRAGMA table_info(candidates)")}
    if "recorded_at" not in columns:
        db.execute("ALTER TABLE candidates ADD COLUMN recorded_at TEXT")
        db.execute("ALTER TABLE candidates ADD COLUMN evidence_kind TEXT DEFAULT 'REPLAY'")
    return db


def load(input_dir):
    universe = pd.read_csv(input_dir / ".universal_daily/research_universe_60.csv")
    lots = dict(zip(universe.code, universe.lot_size.astype(int)))
    names = dict(zip(universe.code, universe.name))
    snapshots = []
    source = input_dir / "forward_ledger.sqlite3"
    if source.exists():
        with closing(sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)) as db:
            snapshots = db.execute("SELECT snapshot_date,code,payload FROM universe_snapshots").fetchall()
        for _, code, payload in snapshots:
            row = json.loads(payload)
            if row.get("lot_size"):
                lots.setdefault(code, int(row["lot_size"]))
                names.setdefault(code, row.get("name", code))
    frames, quality = {}, []
    files = sorted((input_dir / ".raw_daily").glob("HK_*.csv"))
    if not files:
        raise ValueError("Unadjusted prices and corporate actions required; QFQ prices cannot size board-lot trades")
    for path in files:
        frame = pd.read_csv(path)
        if frame.empty:
            continue
        code = str(frame.code.iloc[0])
        frame["time_key"] = pd.to_datetime(frame.time_key, format="mixed").dt.normalize()
        if frame.time_key.duplicated().any():
            raise ValueError(f"Duplicate dates: {code}")
        frame = frame.sort_values("time_key").set_index("time_key")
        price_columns = ["open", "high", "low", "close"]
        if not np.isfinite(frame[price_columns].to_numpy(dtype=float)).all():
            raise ValueError(f"Non-finite price: {code}")
        if (frame[price_columns] <= 0).any().any():
            raise ValueError(f"Non-positive price: {code}")
        frame["share_factor"] = 1.0
        frame["cash_entitlement"] = 0.0
        frame["unsupported_action"] = False
        if code != "HK.800000":
            action_file = input_dir / ".corporate_actions" / path.name
            actions = pd.read_csv(action_file)
            for _, action in actions.iterrows():
                date = pd.Timestamp(action.ex_div_date)
                if date not in frame.index:
                    continue
                factor = action.get("backward_adj_factorA")
                cash = action.get("backward_adj_factorB")
                frame.loc[date, "share_factor"] = float(factor) if pd.notna(factor) else 1.0
                frame.loc[date, "cash_entitlement"] = float(cash) if pd.notna(cash) else 0.0
                complex_action = any(pd.notna(action.get(k)) and float(action[k]) != 0
                                     for k in ("allotment_ratio", "stk_spo_ratio", "spin_off_ratio"))
                frame.loc[date, "unsupported_action"] = complex_action
        quality.append({"code": code, "rows": len(frame), "first": str(frame.index[0].date()),
                        "last": str(frame.index[-1].date()), "known_lot": code in lots,
                        "complex_action_dates": [str(d.date()) for d in frame.index[frame.unsupported_action]]})
        if code == "HK.800000" or code in lots:
            frames[code] = frame
    if "HK.800000" not in frames:
        raise ValueError("HSI calendar unavailable")
    calendar = frames.pop("HK.800000").index
    return frames, calendar, lots, names, snapshots, quality, files


def features(frames):
    output = {}
    for code, frame in frames.items():
        f = frame.copy()
        if "share_factor" in f:
            growth = (f.close * f.share_factor + f.cash_entitlement) / f.close.shift(1)
            c = growth.fillna(1).cumprod() * f.close.iloc[0]
        else:
            c = f.close
        f["signal_close"] = c
        f["ma200"] = c.rolling(200).mean()
        f["ma20"] = c.rolling(20).mean()
        f["mom120"] = c.shift(20) / c.shift(140) - 1
        f["mom60"] = c / c.shift(60) - 1
        f["vol60"] = c.pct_change(fill_method=None).rolling(60).std() * np.sqrt(252)
        f["prior55"] = c.rolling(55).max().shift(1)
        f["turn20"] = f.turnover.rolling(20).mean()
        delta = c.diff()
        gain = delta.clip(lower=0).ewm(alpha=.5, adjust=False, min_periods=2).mean()
        loss = (-delta.clip(upper=0)).ewm(alpha=.5, adjust=False, min_periods=2).mean()
        f["rsi2"] = (100 * gain / (gain + loss)).fillna(50)
        output[code] = f
    return output


def evaluate(row, family):
    if row is None:
        return None, "MISSING_BAR"
    if not np.isfinite(row.get("ma200", np.nan)):
        return None, "INSUFFICIENT_HISTORY"
    if row["close"] < 2 or row["turn20"] < 20_000_000:
        return None, "PRICE_OR_LIQUIDITY"
    close = row.get("signal_close", row["close"])
    if family == "lowvol_control":
        return (-row["vol60"], "ELIGIBLE") if row["vol60"] > 0 else (None, "ZERO_VOLATILITY")
    if family.startswith("momentum"):
        mom = row["mom120" if family == "momentum120" else "mom60"]
        if close > row["ma200"] and mom > 0 and row["vol60"] > 0:
            return mom / row["vol60"], "ELIGIBLE"
    elif family == "breakout55" and close > row["prior55"]:
        return close / row["prior55"] - 1, "ELIGIBLE"
    elif family == "pullback2" and close > row["ma200"] and row["rsi2"] < 10:
        return 100 - row["rsi2"], "ELIGIBLE"
    return None, "RULE_NOT_MET"


def prepare(frames, calendar):
    rows = {code: {date: row for date, row in f.to_dict("index").items()} for code, f in frames.items()}
    ranks = {family: {} for family in FAMILIES}
    for date in calendar:
        for family in FAMILIES:
            ranked = []
            for code in rows:
                score, _ = evaluate(rows[code].get(date), family)
                if score is not None and math.isfinite(score):
                    ranked.append((score, code))
            ranks[family][date] = sorted(ranked, key=lambda x: (-x[0], x[1]))
    return rows, ranks


def quantity(price, budget, lot, slip):
    fill = price * (1 + slip)
    qty = int(budget // (fill * lot)) * lot
    while qty > 0 and qty * fill + order_cost(qty * fill, include_slippage=False) > budget:
        qty -= lot
    return qty


def simulate(rows, ranks, calendar, lots, family, capital, slots, start, end,
             slippage=8, omit=(), memberships=None, liquidate=True):
    dates = calendar[(calendar >= pd.Timestamp(start)) & (calendar <= pd.Timestamp(end))]
    cash, book, pending, fills, curve, trades = capital, {}, None, [], [], []
    receivable, complex_events = 0.0, []
    slip, stale_marks, fees = slippage / 10000, 0, 0.0
    def trade(code, date, side, qty, raw):
        nonlocal cash, fees
        price = raw * (1 + slip if side == "BUY" else 1 - slip)
        fee = order_cost(qty * price, include_slippage=False)
        fees += fee
        fills.append({"date": str(date.date()), "code": code, "side": side,
                      "qty": qty, "price": price, "fee": fee})
        if side == "BUY":
            cash -= qty * price + fee
            book[code] = {"qty": qty, "basis": qty * price + fee, "mark": raw, "age": 0, "income": 0.0}
        else:
            cash += qty * price - fee
            trades.append({"code": code, "pnl": qty * price - fee - book[code]["basis"] + book[code]["income"]})
            del book[code]
    for i, date in enumerate(dates):
        for code, holding in book.items():
            row = rows[code].get(date)
            if row:
                income = holding["qty"] * row.get("cash_entitlement", 0.0)
                receivable += income
                holding["income"] += income
                holding["qty"] *= row.get("share_factor", 1.0)
                if row.get("unsupported_action"):
                    complex_events.append({"code": code, "date": str(date.date())})
        if pending is not None:
            targets, signal_budget = pending
            for code in list(book):
                row = rows[code].get(date)
                if code not in targets and row and row["volume"] > 0:
                    trade(code, date, "SELL", book[code]["qty"], row["open"])
            for code in targets:
                row = rows[code].get(date)
                if code in book or len(book) >= slots or not row or row["volume"] <= 0:
                    continue
                qty = quantity(row["open"], min(cash, signal_budget), lots[code], slip)
                if qty > 0:
                    trade(code, date, "BUY", qty, row["open"])
            pending = None
        for code, h in book.items():
            row = rows[code].get(date)
            if row:
                h["mark"] = row["close"]
            else:
                stale_marks += 1
            h["age"] += 1
        equity = cash + receivable + sum(h["qty"] * h["mark"] for h in book.values())
        curve.append({"date": str(date.date()), "equity": equity, "cash": cash})
        allowed = set(rows) - set(omit) if memberships is None else memberships.get(date, set()) - set(omit)
        ranked = [(s, c) for s, c in ranks[family][date] if c in allowed]
        budget = equity / slots
        affordable = [c for _, c in ranked if date in rows[c]
                      and quantity(rows[c][date]["close"], budget, lots[c], slip) > 0]
        if family in ("momentum120", "momentum60", "lowvol_control"):
            if calendar.get_loc(date) % 20 == 0 or i == 0:
                if memberships is None or date in memberships:
                    pending = (affordable[:slots], budget)
        else:
            targets = []
            for code, h in book.items():
                row = rows[code].get(date)
                exit_now = bool(row and (row.get("signal_close", row["close"]) < row["ma20"] if family == "breakout55"
                                         else row["rsi2"] > 70 or h["age"] >= 5))
                if not exit_now:
                    targets.append(code)
            targets += [c for c in affordable if c not in book][:max(0, slots - len(targets))]
            pending = (targets, budget)
        if cash < -1e-6:
            raise AssertionError("Negative cash")
    unresolved = []
    if len(dates) and liquidate:
        for code in list(book):
            row = rows[code].get(dates[-1])
            if row and row["volume"] > 0:
                trade(code, dates[-1], "SELL", book[code]["qty"], row["close"])
            else:
                unresolved.append(code)
        curve[-1]["equity"] = cash + receivable + sum(h["qty"] * h["mark"] for h in book.values())
        curve[-1]["cash"] = cash
    values = np.array([capital] + [r["equity"] for r in curve])
    ending = values[-1]
    dd = float(np.min(values / np.maximum.accumulate(values) - 1) * 100)
    annual = {}
    prior = capital
    for year in sorted({r["date"][:4] for r in curve}):
        last = [r["equity"] for r in curve if r["date"].startswith(year)][-1]
        annual[year] = (last / prior - 1) * 100
        prior = last
    return {"net_return_pct": (ending / capital - 1) * 100,
            "actual_start": str(dates[0].date()) if len(dates) else None,
            "actual_end": str(dates[-1].date()) if len(dates) else None,
            "market_days": len(dates),
            "annualized_pct": ((ending / capital) ** (252 / max(1, len(dates))) - 1) * 100,
            "max_drawdown_pct": dd, "completed_trades": len(trades), "fees_hkd": fees,
            "dividend_receivable_hkd": receivable, "complex_corporate_actions": complex_events,
            "ending_equity": ending, "annual_returns": annual, "stale_position_days": stale_marks,
            "unresolved_positions": unresolved, "trades": trades, "fills": fills, "curve": curve}


def record_candidates(db, rows, ranks, lots, snapshots, version, now=None):
    now = now or datetime.now(timezone(timedelta(hours=8)))
    for date, code, payload in snapshots:
        db.execute("INSERT OR IGNORE INTO universe VALUES(?,?,?)", (date, code, payload))
    memberships = {}
    for date, code, _ in snapshots:
        memberships.setdefault(pd.Timestamp(date), set()).add(code)
    for date, codes in memberships.items():
        for family in FAMILIES:
            ranked = [code for _, code in ranks[family].get(date, []) if code in codes]
            for code in sorted(codes):
                row = rows.get(code, {}).get(date)
                score, reason = evaluate(row, family)
                rank = ranked.index(code) + 1 if code in ranked else None
                price, lot = (row["close"] if row else None), lots.get(code)
                affordable = int(bool(price and lot and quantity(price, 5790.32, lot, .0008)))
                evidence = ("FORWARD_AFTER_CLOSE" if date.date() == now.date() and now.hour >= 16 else "REPLAY")
                db.execute("INSERT OR IGNORE INTO candidates VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           (version, str(date.date()), family, code, rank, score,
                            int(score is not None), reason, price, lot, affordable,
                            now.isoformat(), evidence))
    return memberships


def settle_outcomes(db, rows, calendar, version):
    records = db.execute("SELECT date,family,code,lot FROM candidates WHERE version=? AND eligible=1", (version,)).fetchall()
    for date, family, code, lot in records:
        if pd.Timestamp(date) not in calendar or not lot:
            continue
        pos = calendar.get_loc(pd.Timestamp(date))
        for horizon in (1, 5, 20):
            if pos + horizon >= len(calendar):
                continue
            entry_date, exit_date = calendar[pos + 1], calendar[pos + horizon]
            entry, exit_row = rows.get(code, {}).get(entry_date), rows.get(code, {}).get(exit_date)
            if not entry or not exit_row or min(entry["volume"], exit_row["volume"]) <= 0:
                continue
            shares, income = float(lot), 0.0
            valid_actions = True
            for held_date in calendar[pos + 2:pos + horizon + 1]:
                row = rows.get(code, {}).get(held_date)
                if row:
                    if row.get("unsupported_action"):
                        valid_actions = False
                        break
                    income += shares * row.get("cash_entitlement", 0.0)
                    shares *= row.get("share_factor", 1.0)
            if not valid_actions:
                continue
            buy, sell = entry["open"] * 1.0008 * lot, exit_row["close"] * .9992 * shares
            basis = buy + order_cost(buy, include_slippage=False)
            ret = ((sell + income - order_cost(sell, include_slippage=False)) / basis - 1) * 100
            db.execute("INSERT OR IGNORE INTO outcomes VALUES(?,?,?,?,?,?,?,?)",
                       (version, date, family, code, horizon, str(entry_date.date()), str(exit_date.date()), ret))


def compact(result):
    return {k: v for k, v in result.items() if k not in ("trades", "fills", "curve")}


def write_summary(report, path):
    labels = {"momentum120": "120日动量", "momentum60": "60日动量", "breakout55": "55日突破",
              "pullback2": "趋势内RSI2回撤", "lowvol_control": "低波动对照"}
    lines = ["# 固定规则策略比较与追踪", "", f"数据截至 {report['last_date']}；研究股票 {report['historical_stock_count']} 只。",
             "", "历史数据属于当前及已留存股票池，仍有幸存者偏差。本报告是研究筛选，不能证明未来盈利。", "",
             "| 策略 | 模拟本金HK$ | 2024起净收益 | 最大回撤 | 完成交易 | 25bps滑点净收益 | 剔除小米净收益 |",
             "|---|---:|---:|---:|---:|---:|---:|"]
    for item in report["families"]:
        r = item["evaluation"]
        lines.append(f"| {labels[item['family']]} | {item['capital']:,.2f} | {r['net_return_pct']:.2f}% | {r['max_drawdown_pct']:.2f}% | {r['completed_trades']} | {item['stress25']['net_return_pct']:.2f}% | {item['without_xiaomi']['net_return_pct']:.2f}% |")
    lines += ["", "现金对照为0%，不含存款利息。完整分年结果、2022—2023诊断结果及数据清单见report.json。",
              "", "收益包含应收分红但不将未确认到账的分红再投资；仍未涵盖历史手数、券商公司行动收费及税款。",
              "", "复杂公司行动或期末缺报价会使对应结果不足以作决策依据。参数没有按本轮结果调优。", "",
              "数据库追踪：", "", "```json", json.dumps(report["tracking"], ensure_ascii=False, indent=2), "```",
              "", "REPLAY是事后重放；FORWARD_AFTER_CLOSE是规则登记后的当日收盘记录。只有后者可以积累新的前向证据。"]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, default=ROOT / ".runtime/strategy-research/input")
    parser.add_argument("--output", type=Path, default=ROOT / ".runtime/strategy-research")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    frames, calendar, lots, names, snapshots, quality, files = load(args.input)
    rows, ranks = prepare(features(frames), calendar)
    version = VERSION + ":" + digest(__file__)[:12]
    manifest = {str(p.relative_to(args.input)): digest(p) for p in files}
    manifest.update({str(p.relative_to(args.input)): digest(p) for p in sorted((args.input / ".corporate_actions").glob("*.csv"))})
    manifest["source_code"] = digest(__file__)
    manifest["protocol"] = digest(ROOT / "STRATEGY_RESEARCH_PROTOCOL.md")
    manifest["universe"] = digest(args.input / ".universal_daily/research_universe_60.csv")
    manifest["snapshots"] = digest(args.input / "forward_ledger.sqlite3")
    run_id = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
    report = {"version": version, "run_id": run_id, "research_only": True,
              "historical_point_in_time": False, "production_eligible": False,
              "cost_model": MODEL_ID, "quality": quality, "families": [],
              "last_date": str(calendar[-1].date()), "historical_stock_count": len(rows)}
    report["calendar_start"] = str(calendar[0].date())
    report["calendar_days"] = len(calendar)
    report["cash_control_net_return_pct"] = 0.0
    for family in FAMILIES:
        for capital, slots in CAPITALS:
            base = simulate(rows, ranks, calendar, lots, family, capital, slots, "2024-01-01", calendar[-1])
            stress = simulate(rows, ranks, calendar, lots, family, capital, slots, "2024-01-01", calendar[-1], slippage=25)
            omit = simulate(rows, ranks, calendar, lots, family, capital, slots, "2024-01-01", calendar[-1], omit=["HK.01810"])
            development = simulate(rows, ranks, calendar, lots, family, capital, slots, "2022-01-01", "2023-12-31")
            report["families"].append({"family": family, "capital": capital,
                "evaluation": compact(base), "stress25": compact(stress),
                "without_xiaomi": compact(omit), "development": compact(development)})
            print(f"{family} capital={capital} return={base['net_return_pct']:.2f}% dd={base['max_drawdown_pct']:.2f}% trades={base['completed_trades']}", flush=True)
    database = args.output / "research.sqlite3"
    if database.exists():
        backup = args.output / ("research.backup-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f") + ".sqlite3")
        with closing(sqlite3.connect(database)) as source, closing(sqlite3.connect(backup)) as target:
            source.backup(target)
    with closing(connect(database)) as db, db:
        memberships = record_candidates(db, rows, ranks, lots, snapshots, version)
        settle_outcomes(db, rows, calendar, version)
        first_forward = db.execute("SELECT MIN(date) FROM candidates WHERE version=? AND evidence_kind='FORWARD_AFTER_CLOSE'", (version,)).fetchone()[0]
        if first_forward:
            forward_memberships = {date: codes for date, codes in memberships.items() if date >= pd.Timestamp(first_forward)}
            for family in FAMILIES:
                for capital, slots in CAPITALS:
                    sim = simulate(rows, ranks, calendar, lots, family, capital, slots,
                                   first_forward, calendar[-1], memberships=forward_memberships, liquidate=False)
                    for fill in sim["fills"]:
                        db.execute("INSERT OR IGNORE INTO simulated_fills VALUES(?,?,?,?,?,?,?,?,?)",
                                   (version, capital, family, *[fill[k] for k in ("date", "code", "side", "qty", "price", "fee")]))
                    for point in sim["curve"]:
                        db.execute("INSERT OR IGNORE INTO simulated_equity VALUES(?,?,?,?,?,?)",
                                   (version, capital, family, point["date"], point["equity"], point["cash"]))
        report["tracking"] = {"universe": db.execute("SELECT COUNT(*) FROM universe").fetchone()[0]}
        report["tracking"].update({table: db.execute(f"SELECT COUNT(*) FROM {table} WHERE version=?", (version,)).fetchone()[0]
                                  for table in ("candidates", "outcomes", "simulated_fills", "simulated_equity")})
        report["tracking"]["evidence_kind"] = dict(db.execute("SELECT evidence_kind,count(*) FROM candidates WHERE version=? GROUP BY evidence_kind", (version,)))
        report["tracking"]["first_forward_date"] = first_forward
        latest = str(calendar[-1].date())
        report["latest_candidates"] = [{"family": f, "code": c, "name": names.get(c,c), "rank": r,
                                         "affordable_small": bool(a)} for f,c,r,a in db.execute(
            "SELECT family,code,rank,affordable_small FROM candidates WHERE version=? AND date=? AND eligible=1 ORDER BY family,rank",
            (version, latest))]
        db.execute("INSERT OR IGNORE INTO runs VALUES(?,?,?,?,?)", (run_id, datetime.now(timezone.utc).isoformat(), version,
                   json.dumps(manifest, sort_keys=True), json.dumps(report, ensure_ascii=False, allow_nan=False)))
    (args.output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    write_summary(report, args.output / "REPORT.md")
    print(json.dumps(report["tracking"]))


if __name__ == "__main__":
    main()

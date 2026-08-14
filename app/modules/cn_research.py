"""Read-only A-share research, execution-aware backtest and validation gate."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd

from app.markets import Signal, ValidationStatus, cn_lot_size, cn_price_limit, get_market_rules, normalize_code

ROOT = Path(__file__).resolve().parents[2]
EVENT_FILE = ROOT / ".runtime" / "contesttrade" / "candidates.json"

DEFAULT_UNIVERSE = [
    "SH.600519", "SH.601318", "SH.600036", "SH.600900", "SH.601899",
    "SH.603993", "SZ.000333", "SZ.000651", "SZ.000858", "SZ.002594",
    "SZ.300750", "SZ.000001",
]


def prepare_features(bars: pd.DataFrame) -> pd.DataFrame:
    x = bars.copy().sort_values("date").reset_index(drop=True)
    close = x.close.astype(float)
    previous = close.shift(1)
    tr = pd.concat([(x.high - x.low), (x.high - previous).abs(),
                    (x.low - previous).abs()], axis=1).max(axis=1)
    x["ma20"] = close.rolling(20).mean()
    x["ma60"] = close.rolling(60).mean()
    x["ma60_slope"] = x.ma60 / x.ma60.shift(20) - 1
    x["prior_high20"] = x.high.shift(1).rolling(20).max()
    x["prior_low20"] = x.low.shift(1).rolling(20).min()
    x["volume_ratio"] = x.volume / x.volume.rolling(20).mean()
    x["atr20"] = tr.rolling(20).mean()
    x["change_pct"] = close.pct_change()
    return x


def _profit_factor(values: pd.Series) -> float:
    gains = values[values > 0].sum(); losses = -values[values <= 0].sum()
    # Keep API output strict-JSON compliant. 999 means gains with no realised
    # losing trade; it is intentionally not represented as Infinity.
    return float(gains / losses) if losses else (999.0 if gains > 0 else 0.0)


def _metrics(trades: pd.DataFrame) -> dict:
    if trades.empty:
        return {"trades": 0, "win_rate_pct": None, "average_return_pct": None,
                "profit_factor": None, "return_pct": 0.0, "max_drawdown_pct": 0.0}
    r = trades.net_return.astype(float)
    equity = pd.concat([pd.Series([1.0]), (1 + r).cumprod()], ignore_index=True)
    dd = equity / equity.cummax() - 1
    return {"trades": int(len(r)), "win_rate_pct": round(float((r > 0).mean() * 100), 2),
            "average_return_pct": round(float(r.mean() * 100), 3),
            "profit_factor": round(_profit_factor(r), 3),
            "return_pct": round(float((equity.iloc[-1] - 1) * 100), 3),
            "max_drawdown_pct": round(float(dd.min() * 100), 3)}


def backtest(bars: pd.DataFrame, *, capital: float = 100_000,
             allocation: float = .20, price_limit: float = .10,
             lot_size: int = 100) -> dict:
    """Fixed, unoptimised trend-breakout rule; signal at close, fill next open."""
    x = prepare_features(bars)
    rules = get_market_rules("CN")
    rows, position = [], None
    for i in range(80, len(x) - 1):
        row, nxt = x.iloc[i], x.iloc[i + 1]
        prev_close = float(row.close)
        next_open = float(nxt.open)
        at_limit_up = next_open >= prev_close * (1 + price_limit - .001)
        at_limit_down = next_open <= prev_close * (1 - price_limit + .001)
        if position is None:
            signal = (row.close > row.prior_high20 and row.close > row.ma60 and
                      row.ma60_slope > 0 and row.volume_ratio >= 1.3)
            if signal and not at_limit_up:
                qty = max(0, int(capital * allocation / next_open) // lot_size * lot_size)
                if not qty: continue
                fee = rules.commission("BUY", qty, next_open)
                position = {"entry_date": nxt.date, "entry_price": next_open,
                            "qty": qty, "buy_fee": fee, "peak": next_open, "bars": 0}
        else:
            position["bars"] += 1
            position["peak"] = max(position["peak"], float(row.high))
            trailing = position["peak"] - 3 * float(row.atr20)
            exit_signal = (row.close < row.prior_low20 or row.close < row.ma60 or
                           row.close < trailing or row.close <= position["entry_price"] * .90)
            # T+1: never sell the lot on its purchase date.
            can_sell = pd.Timestamp(row.date).date() > pd.Timestamp(position["entry_date"]).date()
            if exit_signal and can_sell and not at_limit_down:
                sell_fee = rules.commission("SELL", position["qty"], next_open)
                gross = (next_open - position["entry_price"]) * position["qty"]
                invested = position["entry_price"] * position["qty"] + position["buy_fee"]
                net = (gross - position["buy_fee"] - sell_fee) / invested
                rows.append({**position, "exit_date": nxt.date, "exit_price": next_open,
                             "sell_fee": sell_fee, "net_return": net})
                position = None
    trades = pd.DataFrame(rows)
    if not trades.empty:
        for col in ("entry_date", "exit_date"):
            trades[col] = pd.to_datetime(trades[col]).dt.strftime("%Y-%m-%d")
    return {"metrics": _metrics(trades),
            "trades": trades.replace({np.nan: None}).to_dict("records") if not trades.empty else [],
            "assumptions": {"signal": "收盘确认，下一交易日开盘成交", "settlement": "T+1",
                            "lot_size": lot_size, "price_limit": price_limit,
                            "buy_commission": rules.buy_commission,
                            "sell_stamp_tax": rules.sell_stamp_tax}}


def validate(bars: pd.DataFrame, code: str = "SH.000000") -> dict:
    x = bars.sort_values("date").reset_index(drop=True)
    if len(x) < 500:
        return {"status": ValidationStatus.RESEARCH_ONLY.value, "passed": False,
                "reason": "历史数据少于500个交易日", "segments": {}}
    n = len(x); cut1, cut2 = int(n * .60), int(n * .80)
    # Warm-up is carried into each later segment, while trades are attributed by entry date.
    report = backtest(x, price_limit=cn_price_limit(code), lot_size=cn_lot_size(code))
    all_trades = pd.DataFrame(report["trades"])
    boundaries = {"train": (x.date.iloc[0], x.date.iloc[cut1 - 1]),
                  "validation": (x.date.iloc[cut1], x.date.iloc[cut2 - 1]),
                  "blind": (x.date.iloc[cut2], x.date.iloc[-1])}
    segments = {}
    for name, (start, end) in boundaries.items():
        if all_trades.empty: selected = pd.DataFrame(columns=["net_return"])
        else:
            dates = pd.to_datetime(all_trades.entry_date)
            selected = all_trades[(dates >= pd.Timestamp(start)) & (dates <= pd.Timestamp(end))]
        segments[name] = _metrics(selected)
    enough = (segments["train"]["trades"] >= 10 and segments["validation"]["trades"] >= 4
              and segments["blind"]["trades"] >= 4 and report["metrics"]["trades"] >= 20)
    quality = all((m["average_return_pct"] or 0) > 0 and (m["profit_factor"] or 0) >= 1.2
                  and m["max_drawdown_pct"] >= -25 for m in segments.values())
    passed = bool(enough and quality)
    return {"status": (ValidationStatus.PAPER if passed else ValidationStatus.RESEARCH_ONLY).value,
            "passed": passed,
            "reason": ("通过历史三段门禁，进入前向模拟，尚不允许真实交易" if passed
                       else "未同时通过训练、验证和盲测门槛"),
            "segments": segments, "full": report["metrics"],
            "boundaries": {k: [str(pd.Timestamp(a).date()), str(pd.Timestamp(b).date())]
                           for k, (a, b) in boundaries.items()}}


def latest_signal(code: str, bars: pd.DataFrame, validation: dict | None = None) -> dict:
    normalized = normalize_code(code, "CN")
    x = prepare_features(bars)
    row = x.iloc[-1]
    observed = "WAIT"; reason = "未出现放量突破"
    if row.close > row.prior_high20 and row.close > row.ma60 and row.ma60_slope > 0 and row.volume_ratio >= 1.3:
        observed, reason = "BUY", "上升趋势中放量突破20日高点"
    elif row.close < row.ma60:
        observed, reason = "RISK", "收盘价低于60日均线"
    gate = validation or {"status": ValidationStatus.RESEARCH_ONLY.value, "passed": False}
    formal = observed if gate.get("status") == ValidationStatus.ACTIVE.value else "WAIT"
    signal = Signal(f"cn_trend_breakout_v1:{normalized}", normalized, "CN", formal,
                    ValidationStatus(gate.get("status", ValidationStatus.RESEARCH_ONLY.value)),
                    reason + ("；验证门禁未激活" if formal != observed else ""),
                    str(pd.Timestamp(row.date).date()), float(row.close), float(row.prior_high20),
                    float(min(row.prior_low20, row.ma60)), 0)
    out = signal.to_dict(); out["observed_action"] = observed
    out["indicators"] = {k: round(float(row[k]), 3) for k in
                         ("ma20", "ma60", "prior_high20", "prior_low20", "volume_ratio")}
    return out


def load_event_candidates() -> dict:
    if not EVENT_FILE.exists():
        return {"status": "UNAVAILABLE", "source": "ContestTrade",
                "items": [], "reason": "尚未导入ContestTrade候选文件"}
    try:
        raw = json.loads(EVENT_FILE.read_text(encoding="utf-8"))
        items = raw if isinstance(raw, list) else raw.get("items", [])
        clean = []
        for item in items:
            try: code = normalize_code(item.get("code", ""), "CN")
            except ValueError: continue
            clean.append({"code": code, "name": item.get("name"),
                          "score": item.get("score"), "catalysts": item.get("catalysts", []),
                          "risks": item.get("risks", []), "action": "RESEARCH",
                          "validation_status": ValidationStatus.RESEARCH_ONLY.value})
        return {"status": "AVAILABLE", "source": "ContestTrade", "items": clean}
    except Exception as exc:  # noqa: BLE001
        return {"status": "FAILED", "source": "ContestTrade", "items": [], "reason": str(exc)}


def save_event_candidates(payload: dict | list) -> int:
    items = payload if isinstance(payload, list) else payload.get("items", [])
    if not isinstance(items, list): raise ValueError("items必须是数组")
    EVENT_FILE.parent.mkdir(parents=True, exist_ok=True)
    EVENT_FILE.write_text(json.dumps({"items": items}, ensure_ascii=False, indent=2), encoding="utf-8")
    return len(items)


def scan(router, codes: Iterable[str] | None = None) -> dict:
    pool = [normalize_code(c, "CN") for c in (codes or DEFAULT_UNIVERSE)]
    candidates, errors = [], []
    for code in pool:
        bars, err = router.daily_bars(code, count=520)
        if err or bars is None or len(bars) < 80:
            errors.append(f"{code}: {err or '历史数据不足'}"); continue
        gate = validate(bars, code)
        item = latest_signal(code, bars, gate)
        item["validation"] = gate
        candidates.append(item)
    candidates.sort(key=lambda x: (x.get("observed_action") == "BUY",
                                    x.get("indicators", {}).get("volume_ratio", 0)), reverse=True)
    return {"market": "CN", "benchmark": get_market_rules("CN").benchmark,
            "universe_size": len(pool), "evaluated": len(candidates),
            "candidates": candidates, "event_candidates": load_event_candidates(),
            "errors": errors, "execution_mode": "READ_ONLY_RESEARCH"}

"""Cross-sectional HK rotation research with annual walk-forward validation.

This is research-only.  Signals are formed at the close and all orders execute
at the next open with board lots, fees and slippage.  Parameters for each test
year are selected only from the preceding three calendar years.
"""
from __future__ import annotations

import itertools
import json
import math
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.hk_costs import order_cost
from scripts.run_walkforward_daily import metrics_from_folds

DATA = ROOT / ".research_daily_150"


def load_data():
    universe = pd.read_csv(DATA / "universe.csv")
    # Exclude RMB dual-counter 8xxxx codes so one issuer cannot enter twice.
    universe = universe[~universe.code.astype(str).str.match(r"HK\.8\d{4}$")]
    lots = {str(row.code): int(row.lot_size) for _, row in universe.iterrows()}
    names = {str(row.code): str(row["name"]) for _, row in universe.iterrows()}
    frames = {}
    for path in DATA.glob("HK_*.csv"):
        frame = pd.read_csv(path)
        if frame.empty or "code" not in frame:
            continue
        code = str(frame.code.iloc[-1])
        if code not in lots or len(frame) < 260:
            continue
        frame.time_key = pd.to_datetime(frame.time_key)
        frame = frame.sort_values("time_key").drop_duplicates("time_key", keep="last").set_index("time_key")
        close = frame.close.astype(float)
        frame["turn20"] = frame.turnover.astype(float).rolling(20).mean()
        frames[code] = frame
    index = pd.read_csv(DATA / "HK_800000.csv")
    index.time_key = pd.to_datetime(index.time_key)
    index = index.sort_values("time_key").drop_duplicates("time_key", keep="last").set_index("time_key")
    return frames, index, lots, names


@dataclass(frozen=True)
class Params:
    family: str
    lookback: int
    skip: int
    rebalance: int
    top_n: int
    market_ma: int


def grid() -> list[Params]:
    momentum = [Params(*x) for x in itertools.product(
        ("momentum", "risk_adjusted_momentum"), (60, 120, 180), (0, 20),
        (10, 20), (2, 4), (120, 200))]
    reversal = [Params(*x) for x in itertools.product(
        ("short_reversal",), (5, 20), (0,), (5, 10), (2, 4), (120, 200))]
    high_52w = [Params(*x) for x in itertools.product(
        ("high_52w",), (120, 180), (0,), (10, 20), (2, 4), (120, 200))]
    return momentum + reversal + high_52w


def prepare(data: dict[str, pd.DataFrame], index: pd.DataFrame) -> None:
    for frame in data.values():
        close = frame.close.astype(float)
        frame["ret20vol"] = close.pct_change().rolling(60).std() * np.sqrt(252)
        for n in (60, 120, 180, 200):
            frame[f"rot_ma{n}"] = close.rolling(n).mean()
    for n in (120, 200):
        index[f"rot_ma{n}"] = index.close.rolling(n).mean()


def score(frame: pd.DataFrame, i: int, p: Params) -> float | None:
    if i < max(220, p.lookback + p.skip):
        return None
    row = frame.iloc[i]
    past = float(frame.close.iloc[i - p.skip]) if p.skip else float(row.close)
    base = float(frame.close.iloc[i - p.lookback - p.skip])
    momentum = past / base - 1
    liquid = float(row.turn20) >= 100_000_000 and float(row.close) >= 2
    trend = float(row.close) > float(row.rot_ma200)
    if not liquid or not trend or not np.isfinite(row.ret20vol):
        return None
    if p.family == "short_reversal":
        return -momentum if momentum < 0 else None
    if p.family == "high_52w":
        prior_high = float(frame.high.iloc[max(0, i - 252):i + 1].max())
        proximity = float(row.close) / prior_high
        return proximity if momentum > 0 else None
    if momentum <= 0:
        return None
    return momentum if p.family == "momentum" else momentum / max(.08, float(row.ret20vol))


def empty_result(capital: float = 20_000.) -> dict:
    return {"ending": capital, "return_pct": 0., "max_dd_pct": 0.,
            "profit_factor": 0., "trades": [], "curve": []}


def simulate(data, index, lots, p: Params, start, end, capital=20_000.,
             slippage_bps=8., omit=None, daily_market_exit: bool = False) -> dict:
    omit = set(omit or [])
    dates = [d for d in index.index if start <= d <= end]
    if not dates:
        return empty_result(capital)
    cash, positions, pending_targets = float(capital), {}, None
    curve, trades, slip = [], [], slippage_bps / 10_000
    for day_no, date in enumerate(dates):
        if pending_targets is not None:
            targets = set(pending_targets)
            for code in list(positions):
                if code in targets or date not in data[code].index:
                    continue
                px = float(data[code].loc[date].open) * (1 - slip)
                gross = positions[code]["qty"] * px
                fee = order_cost(gross, include_slippage=False)
                cash += gross - fee
                trades.append({"code": code, "entry": positions[code]["entry_date"],
                               "exit": str(date.date()),
                               "pnl": gross - fee - positions[code]["basis"]})
                del positions[code]
            equity_open = cash + sum(
                h["qty"] * float(data[c].loc[date].open)
                for c, h in positions.items() if date in data[c].index)
            budget = equity_open / max(1, p.top_n)
            for code in pending_targets:
                if code in positions or code in omit or date not in data[code].index:
                    continue
                px = float(data[code].loc[date].open) * (1 + slip)
                lot = lots[code]
                qty = int(budget // (px * lot)) * lot
                if qty <= 0:
                    continue
                gross = qty * px
                fee = order_cost(gross, include_slippage=False)
                if gross + fee <= cash:
                    cash -= gross + fee
                    positions[code] = {"qty": qty, "basis": gross + fee,
                                       "entry_date": str(date.date())}
            pending_targets = None
        value = cash + sum(h["qty"] * float(data[c].loc[date].close)
                           for c, h in positions.items() if date in data[c].index)
        curve.append((date, value))
        market_ok = (date in index.index and np.isfinite(index.loc[date, f"rot_ma{p.market_ma}"])
                     and float(index.loc[date].close) > float(index.loc[date, f"rot_ma{p.market_ma}"]))
        if daily_market_exit and not market_ok and positions and date != dates[-1]:
            pending_targets = []
        elif day_no % p.rebalance == 0 and date != dates[-1]:
            ranked = []
            if market_ok:
                for code, frame in data.items():
                    if code in omit or date not in frame.index:
                        continue
                    s = score(frame, frame.index.get_loc(date), p)
                    if s is not None:
                        ranked.append((s, code))
            pending_targets = [code for _, code in sorted(ranked, reverse=True)[:p.top_n]]
    last = dates[-1]
    for code, holding in list(positions.items()):
        if last not in data[code].index:
            continue
        px = float(data[code].loc[last].close) * (1 - slip)
        gross = holding["qty"] * px
        fee = order_cost(gross, include_slippage=False)
        cash += gross - fee
        trades.append({"code": code, "entry": holding["entry_date"],
                       "exit": str(last.date()), "pnl": gross - fee - holding["basis"]})
    pnl = [x["pnl"] for x in trades]
    gains, losses = sum(x for x in pnl if x > 0), -sum(x for x in pnl if x < 0)
    values = np.array([v for _, v in curve], dtype=float)
    return {"ending": cash, "return_pct": (cash / capital - 1) * 100,
            "max_dd_pct": float((values / np.maximum.accumulate(values) - 1).min() * 100),
            "profit_factor": gains / losses if losses else (999 if gains else 0),
            "trades": trades, "curve": curve}


def main() -> None:
    data, index, lots, names = load_data()
    prepare(data, index)
    years = range(2022, 2027)
    folds, selections = [], []
    for year in years:
        train_start, train_end = pd.Timestamp(f"{year-3}-01-01"), pd.Timestamp(f"{year-1}-12-31")
        ranked = []
        for p in grid():
            result = simulate(data, index, lots, p, train_start, train_end)
            score_value = result["return_pct"] + result["max_dd_pct"]
            if len(result["trades"]) < 20 or result["profit_factor"] < 1.10 or result["max_dd_pct"] < -20:
                score_value = -999
            ranked.append((score_value, p, result))
        eligible = [x for x in ranked if x[0] > -999]
        if not eligible:
            chosen, train, test = None, empty_result(), empty_result()
        else:
            _, chosen, train = max(eligible, key=lambda x: x[0])
            test = simulate(data, index, lots, chosen, pd.Timestamp(f"{year}-01-01"), pd.Timestamp(f"{year}-12-31"))
        folds.append(test)
        selections.append({"test_year": year, "params": asdict(chosen) if chosen else None,
                           "train": {"return_pct": train["return_pct"], "pf": train["profit_factor"],
                                     "dd": train["max_dd_pct"], "n": len(train["trades"])},
                           "test": {"return_pct": test["return_pct"], "pf": test["profit_factor"],
                                    "dd": test["max_dd_pct"], "n": len(test["trades"])}})
        print(selections[-1], flush=True)
    base = metrics_from_folds(folds)
    stress_folds = []
    for year, selected in zip(years, selections):
        stress_folds.append(empty_result() if selected["params"] is None else simulate(
            data, index, lots, Params(**selected["params"]), pd.Timestamp(f"{year}-01-01"),
            pd.Timestamp(f"{year}-12-31"), slippage_bps=25))
    stress = metrics_from_folds(stress_folds)
    traded = sorted({x["code"] for x in base["trades"]})
    yearly_positive = sum(x["test"]["return_pct"] > 0 for x in selections)
    gate = {"oos_trades_40": base["trade_count"] >= 40,
            "oos_pf_1_30": base["profit_factor"] >= 1.30,
            "oos_return_10pct": base["return_pct"] >= 10,
            "max_drawdown_under_15pct": base["max_drawdown_pct"] >= -15,
            "positive_years_4_of_5": yearly_positive >= 4,
            "stress_25bps_positive": stress["return_pct"] > 0}
    gate["passed"] = all(gate.values())
    report = {"research_only": True, "bias_warning": "current liquid universe creates survivorship/selection bias",
              "method": "annual walk-forward; prior 3 years train, next year untouched",
              "universe_loaded": len(data), "grid_count": len(grid()), "folds": selections,
              "oos": {k: v for k, v in base.items() if k != "trades"},
              "stress_25bps": {k: v for k, v in stress.items() if k != "trades"},
              "distinct_stocks": len(traded), "stocks": [{"code": c, "name": names.get(c, c)} for c in traded],
              "gate": gate, "trades": base["trades"]}
    out = ROOT / "data" / "cross_sectional_rotation_results.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"oos": report["oos"], "stress": report["stress_25bps"],
                      "gate": gate}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

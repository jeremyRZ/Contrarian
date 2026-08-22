"""Backtest directional Xiaomi option structures using official HKEX settlements."""
from __future__ import annotations

import re
from dataclasses import dataclass
from math import erf, exp, log, sqrt
from pathlib import Path

import numpy as np
import pandas as pd


EXPIRY_RE = re.compile(r"EXPIRATION DATE\s*:\s*(\d{2} [A-Z]{3} \d{2})")
ROW_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s+(.+)$")


@dataclass(frozen=True)
class OptionTrade:
    entry_date: str
    exit_date: str
    direction: int
    spot: float
    realized_vol_20d: float


def parse_dtop(path: Path) -> pd.DataFrame:
    expiry = None
    rows = []
    for line in path.read_text(encoding="latin-1").splitlines():
        match = EXPIRY_RE.search(line)
        if match:
            expiry = pd.to_datetime(match.group(1), format="%d %b %y")
            continue
        match = ROW_RE.match(line)
        if not match or expiry is None:
            continue
        tokens = match.group(2).replace(",", "").split()
        if len(tokens) != 14:
            continue
        values = [float(match.group(1))] + [float(v) if v != "-" else np.nan for v in tokens]
        rows.append({"expiry": expiry, "strike": values[0],
                     "call_oi": values[1], "call_turnover": values[4], "call_settle": values[6],
                     "put_oi": values[8], "put_turnover": values[11], "put_settle": values[13]})
    return pd.DataFrame(rows).drop_duplicates(["expiry", "strike"], keep="last")


def build_trades(stock: pd.DataFrame, max_hold: int = 20) -> list[OptionTrade]:
    x = stock.copy()
    x["time_key"] = pd.to_datetime(x.time_key)
    x = x[x.time_key >= "2021-01-01"].sort_values("time_key").reset_index(drop=True)
    x["mom20"] = x.close.pct_change(20)
    x["state"] = (x.mom20 > .05).astype(int) - (x.mom20 < -.05).astype(int)
    trades = []
    for i in range(21, len(x) - 1):
        state = int(x.loc[i, "state"])
        if state == 0 or state == int(x.loc[i - 1, "state"]):
            continue
        entry_i = i + 1
        exit_i = min(entry_i + max_hold, len(x) - 1)
        for j in range(entry_i, exit_i):
            if int(x.loc[j, "state"]) != state:
                exit_i = j
                break
        trades.append(OptionTrade(str(x.loc[entry_i, "time_key"].date()),
                                  str(x.loc[exit_i, "time_key"].date()), state,
                                  float(x.loc[entry_i, "close"]),
                                  float(x.close.pct_change().iloc[max(0, entry_i - 19):entry_i + 1].std()
                                        * np.sqrt(252))))
    return trades


def _nearest(frame: pd.DataFrame, target: float) -> pd.Series | None:
    if frame.empty:
        return None
    return frame.loc[(frame.strike - target).abs().idxmin()]


def _norm_cdf(value: float) -> float:
    return .5 * (1 + erf(value / sqrt(2)))


def _bs_price_delta(spot: float, strike: float, years: float, vol: float,
                    side: str, rate: float = .03) -> tuple[float, float]:
    d1 = (log(spot / strike) + (rate + .5 * vol * vol) * years) / (vol * sqrt(years))
    d2 = d1 - vol * sqrt(years)
    if side == "call":
        return spot * _norm_cdf(d1) - strike * exp(-rate * years) * _norm_cdf(d2), _norm_cdf(d1)
    return strike * exp(-rate * years) * _norm_cdf(-d2) - spot * _norm_cdf(-d1), _norm_cdf(d1) - 1


def implied_vol_delta(price: float, spot: float, strike: float, days: int,
                      side: str) -> tuple[float, float] | None:
    if price <= 0 or spot <= 0 or strike <= 0 or days <= 0:
        return None
    years = days / 365
    low, high = .01, 3.0
    low_price = _bs_price_delta(spot, strike, years, low, side)[0]
    high_price = _bs_price_delta(spot, strike, years, high, side)[0]
    if not low_price <= price <= high_price:
        return None
    for _ in range(60):
        mid = (low + high) / 2
        if _bs_price_delta(spot, strike, years, mid, side)[0] < price:
            low = mid
        else:
            high = mid
    vol = (low + high) / 2
    return vol, _bs_price_delta(spot, strike, years, vol, side)[1]


def evaluate_trade(trade: OptionTrade, entry: pd.DataFrame, exit_: pd.DataFrame,
                   family: str, slippage_pct: float = 5.0) -> dict | None:
    entry_date = pd.Timestamp(trade.entry_date)
    expiries = entry[(entry.expiry - entry_date).dt.days.between(25, 60)].expiry.unique()
    if not len(expiries):
        return None
    expiry = min(expiries)
    e = entry[entry.expiry == expiry]
    z = exit_[exit_.expiry == expiry]
    if z.empty:
        return None
    side = "call" if trade.direction == 1 else "put"
    credit_structure = False
    if family == "single_atm":
        targets = [trade.spot]
        signs = [1]
    elif family == "single_otm5":
        targets = [trade.spot * (1.05 if trade.direction == 1 else .95)]
        signs = [1]
    elif family == "vertical10":
        targets = [trade.spot, trade.spot * (1.10 if trade.direction == 1 else .90)]
        signs = [1, -1]
    elif family == "single_value_delta":
        side_rows = []
        dte = int((pd.Timestamp(expiry) - entry_date).days)
        for _, row in e.iterrows():
            price = float(row[f"{side}_settle"])
            iv_delta = implied_vol_delta(price, trade.spot, float(row.strike), dte, side)
            if iv_delta is None:
                continue
            iv, delta = iv_delta
            if (float(row[f"{side}_turnover"]) >= 10 and float(row[f"{side}_oi"]) >= 100
                    and .35 <= abs(delta) <= .60
                    and iv <= trade.realized_vol_20d * 1.15):
                side_rows.append((abs(abs(delta) - .45), float(row.strike), iv, delta))
        if not side_rows:
            return None
        _, strike, selected_iv, selected_delta = min(side_rows)
        targets, signs = [strike], [1]
    elif family == "credit_vertical10":
        credit_structure = True
        if trade.direction == 1:
            side, targets = "put", [trade.spot, trade.spot * .90]
        else:
            side, targets = "call", [trade.spot, trade.spot * 1.10]
        signs = [-1, 1]
    else:
        raise ValueError(family)
    legs = []
    for target, sign in zip(targets, signs):
        erow = _nearest(e, target)
        if erow is None:
            return None
        xrow = z[z.strike == erow.strike]
        if xrow.empty:
            return None
        entry_price = float(erow[f"{side}_settle"])
        exit_price = float(xrow.iloc[0][f"{side}_settle"])
        turnover = float(erow[f"{side}_turnover"])
        oi = float(erow[f"{side}_oi"])
        if entry_price <= 0 or exit_price < 0 or turnover < 10 or oi < 100:
            return None
        # Long legs pay above settlement; short legs receive below settlement.
        fill_entry = entry_price * (1 + sign * slippage_pct / 100)
        fill_exit = exit_price * (1 - sign * slippage_pct / 100)
        legs.append({"strike": float(erow.strike), "sign": sign,
                     "entry": fill_entry, "exit": fill_exit})
    debit = sum(leg["sign"] * leg["entry"] for leg in legs)
    exit_value = sum(leg["sign"] * leg["exit"] for leg in legs)
    if credit_structure:
        width = abs(legs[0]["strike"] - legs[1]["strike"])
        max_risk = width + debit
        if debit >= 0 or max_risk <= .01:
            return None
        pnl = exit_value - debit
        return_pct = pnl / max_risk * 100
    else:
        if debit <= .01:
            return None
        pnl = exit_value - debit
        max_risk = debit
        return_pct = pnl / debit * 100
    return {"entry_date": trade.entry_date, "exit_date": trade.exit_date,
            "direction": trade.direction, "family": family, "expiry": str(pd.Timestamp(expiry).date()),
            "legs": legs, "debit": debit, "max_risk": max_risk,
            "pnl": pnl, "return_pct": return_pct,
            "entry_iv_pct": selected_iv * 100 if family == "single_value_delta" else None,
            "entry_delta": selected_delta if family == "single_value_delta" else None,
            "realized_vol_20d_pct": trade.realized_vol_20d * 100}


def aggregate(results: list[dict]) -> dict:
    if not results:
        return {"count": 0}
    r = np.array([x["return_pct"] / 100 for x in results])
    clipped = np.maximum(r, -1)
    curve = np.cumprod(1 + clipped * .03)
    dd = curve / np.maximum.accumulate(curve) - 1
    return {"count": len(results), "win_rate": float(np.mean(r > 0)),
            "mean_return_on_debit_pct": float(r.mean() * 100),
            "median_return_on_debit_pct": float(np.median(r) * 100),
            "profit_factor": float(r[r > 0].sum() / -r[r < 0].sum()) if np.any(r < 0) else 999.0,
            "portfolio_return_pct": float((curve[-1] - 1) * 100),
            "max_drawdown_pct": float(dd.min() * 100)}

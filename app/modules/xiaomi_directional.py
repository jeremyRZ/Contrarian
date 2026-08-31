"""Daily long/flat/short regime signals for Xiaomi, with next-open execution."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from app.hk_costs import effective_rate

from . import signal_governance


@dataclass(frozen=True)
class DirectionalParams:
    family: str
    fast: int
    slow: int
    threshold: float = 0.0


def prepare(stock: pd.DataFrame, index: pd.DataFrame) -> pd.DataFrame:
    s, i = stock.copy(), index.copy()
    s["time_key"] = pd.to_datetime(s["time_key"])
    i["time_key"] = pd.to_datetime(i["time_key"])
    x = s.merge(i[["time_key", "close"]].rename(columns={"close": "idx_close"}),
                on="time_key", how="left").sort_values("time_key").reset_index(drop=True)
    c = x.close.astype(float)
    ic = x.idx_close.astype(float)
    for n in (10, 20, 40, 60, 80, 120, 160, 200):
        x[f"ma{n}"] = c.rolling(n).mean()
    for n in (20, 60, 120):
        x[f"mom{n}"] = c.pct_change(n)
        x[f"rel{n}"] = c.pct_change(n) - ic.pct_change(n)
    x["vol20"] = c.pct_change().rolling(20).std() * np.sqrt(252)
    return x.dropna(subset=["open", "close", "idx_close", "ma200", "mom120", "rel120", "vol20"]).reset_index(drop=True)


def desired_state(row: pd.Series, params: DirectionalParams) -> int:
    if params.family == "dual_ma":
        spread = row[f"ma{params.fast}"] / row[f"ma{params.slow}"] - 1
        return 1 if spread > params.threshold else -1 if spread < -params.threshold else 0
    if params.family == "momentum":
        mom = row[f"mom{params.slow}"]
        return 1 if mom > params.threshold else -1 if mom < -params.threshold else 0
    if params.family == "ensemble":
        votes = [np.sign(row.mom20), np.sign(row.mom60), np.sign(row.mom120),
                 np.sign(row.close / row.ma20 - 1), np.sign(row.ma60 / row.ma200 - 1),
                 np.sign(row.rel60)]
        score = float(np.sum(votes))
        return 1 if score >= params.threshold else -1 if score <= -params.threshold else 0
    raise ValueError(params.family)


def evaluate(x: pd.DataFrame, params: DirectionalParams, *, allocation: float = 0.30,
             fee_bps: float | None = None, slippage_bps: float = 8,
             capital_hkd: float = 20_000,
             annual_borrow_pct: float = 8) -> dict:
    """Mark-to-market from next open to next open; signal never uses future bars."""
    desired = x.apply(lambda r: desired_state(r, params), axis=1).astype(int)
    position = desired.shift(1).fillna(0).astype(int)
    next_open_return = x.open.astype(float).shift(-1) / x.open.astype(float) - 1
    turnover = position.diff().abs().fillna(position.abs())
    rate = (effective_rate(capital_hkd * allocation, slippage_bps)
            if fee_bps is None else (fee_bps + slippage_bps) / 10_000)
    costs = turnover * rate * allocation
    borrow = (position < 0).astype(float) * annual_borrow_pct / 100 / 252 * allocation
    returns = position * next_open_return * allocation - costs - borrow
    returns = returns.iloc[:-1]
    equity = (1 + returns).cumprod()
    drawdown = equity / equity.cummax() - 1
    downside = returns[returns < 0]
    std = returns.std(ddof=1)
    down_std = downside.std(ddof=1)
    entries = ((position != position.shift(1)) & (position != 0)).iloc[:-1]
    gross_win = returns[returns > 0].sum()
    gross_loss = -returns[returns < 0].sum()
    return {"params": asdict(params), "count": int(entries.sum()),
            "return_pct": float((equity.iloc[-1] - 1) * 100),
            "annualized_pct": float((equity.iloc[-1] ** (252 / len(returns)) - 1) * 100),
            "sharpe": float(returns.mean() / std * np.sqrt(252)) if std else 0.0,
            "sortino": float(returns.mean() / down_std * np.sqrt(252)) if down_std else 0.0,
            "profit_factor": float(gross_win / gross_loss) if gross_loss else 999.0,
            "max_drawdown_pct": float(drawdown.min() * 100),
            "long_days": int((position.iloc[:-1] > 0).sum()),
            "short_days": int((position.iloc[:-1] < 0).sum()),
            "flat_days": int((position.iloc[:-1] == 0).sum())}


def current_signal(x: pd.DataFrame, params: DirectionalParams) -> dict:
    row = x.iloc[-1]
    state = desired_state(row, params)
    result = signal_governance.research_observation(
        "xiaomi_momentum_20d_v1", as_of=str(row.time_key.date()), state=state)
    result["price"] = float(row.close)
    return result


def live_status(client, cfg: dict | None = None) -> tuple[dict | None, str | None]:
    """Build a read-only transition alert from the latest Futu daily bars."""
    settings = (cfg or {}).get("xiaomi_directional", {}) or {}
    if not bool(settings.get("enabled", True)):
        return None, "小米方向提醒已禁用"
    threshold = float(settings.get("threshold_pct", 5.0)) / 100
    end = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=180)).strftime("%Y-%m-%d")
    bars, err = client.history_kline("HK.01810", max_count=200, start=start, end=end)
    if err or bars is None or len(bars) < 22:
        return None, err or "小米日线不足22条"
    x = bars.copy().sort_values("time_key").drop_duplicates("time_key", keep="last")
    x["time_key"] = pd.to_datetime(x["time_key"])
    age_days = (pd.Timestamp(datetime.now().date()) - x.time_key.iloc[-1].normalize()).days
    if age_days > 7:
        return None, f"小米行情已过期{age_days}天，禁止生成提醒"
    x["mom20"] = x.close.astype(float).pct_change(20)
    latest, previous = x.iloc[-1], x.iloc[-2]

    def state(v: float) -> int:
        return 1 if v > threshold else -1 if v < -threshold else 0

    current_state, previous_state = state(float(latest.mom20)), state(float(previous.mom20))
    transition = current_state != previous_state
    shortability = {"confirmed": False, "available_volume": None, "rate": None}
    if current_state == -1:
        snap, snap_err = client.market_snapshot(["HK.01810"])
        if not snap_err and snap is not None and not snap.empty:
            row = snap.iloc[0]
            enabled = row.get("enable_short_sell")
            available = row.get("short_available_volume")
            rate = row.get("short_sell_rate")
            shortability = {
                "confirmed": bool(enabled is True and pd.notna(available) and float(available) >= 200),
                "available_volume": float(available) if pd.notna(available) else None,
                "rate": float(rate) if pd.notna(rate) else None,
            }
    result = signal_governance.research_observation(
        "xiaomi_momentum_20d_v1", as_of=str(latest.time_key.date()), state=current_state,
        value_pct=float(latest.mom20 * 100),
    )
    result.update({
        "price": float(latest.close), "momentum_20d_pct": float(latest.mom20 * 100),
        "threshold_pct": threshold * 100, "previous_state": previous_state,
        "transition": transition, "shortability": shortability,
        "execution": "研究观察；不生成买卖建议、不推送交易信号、不自动下单",
        "validation": {"test_return_pct": 4.6805, "test_sharpe": 0.2858,
                       "test_max_drawdown_pct": -13.8339,
                       "test_profit_factor": 1.0591},
    })
    return result, None


def notification(status: dict) -> tuple[str, str] | None:
    """Research models are structurally unable to create trade notifications."""
    return None

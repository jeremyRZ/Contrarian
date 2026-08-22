"""Daily long/flat/short regime signals for Xiaomi, with next-open execution."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta

import numpy as np
import pandas as pd


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
             fee_bps: float = 12, slippage_bps: float = 8,
             annual_borrow_pct: float = 8) -> dict:
    """Mark-to-market from next open to next open; signal never uses future bars."""
    desired = x.apply(lambda r: desired_state(r, params), axis=1).astype(int)
    position = desired.shift(1).fillna(0).astype(int)
    next_open_return = x.open.astype(float).shift(-1) / x.open.astype(float) - 1
    turnover = position.diff().abs().fillna(position.abs())
    costs = turnover * (fee_bps + slippage_bps) / 10_000 * allocation
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
    return {"as_of": str(row.time_key.date()), "state": state,
            "action": "BUY" if state == 1 else "SELL" if state == -1 else "WAIT",
            "price": float(row.close)}


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
    action = "BUY" if current_state == 1 else "SELL" if current_state == -1 else "WAIT"
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
    return {"id": "xiaomi_momentum_20d_v1", "as_of": str(latest.time_key.date()),
            "price": float(latest.close), "momentum_20d_pct": float(latest.mom20 * 100),
            "threshold_pct": threshold * 100, "state": current_state,
            "previous_state": previous_state, "transition": transition, "action": action,
            "shortability": shortability, "execution": "下一交易日开盘前人工复核；不自动下单",
            "validation": {"test_return_pct": 4.6805, "test_sharpe": 0.2858,
                           "test_max_drawdown_pct": -13.8339,
                           "test_profit_factor": 1.0591}}, None


def notification(status: dict) -> tuple[str, str] | None:
    """Return fingerprint/text only for a fresh BUY or SELL state transition."""
    if not status.get("transition") or status.get("action") not in {"BUY", "SELL"}:
        return None
    direction = "做多" if status["action"] == "BUY" else "做空"
    borrow = ""
    if status["action"] == "SELL" and not status["shortability"]["confirmed"]:
        borrow = "\n⚠️ Futu 未确认券源：只做空头候选提醒，确认可借数量及费率后才可执行。"
    fp = f"xiaomi-directional:{status['as_of']}:{status['action']}"
    text = (f"**小米 {direction}状态切换（{status['action']}）**\n"
            f"收盘价：{status['price']:.2f} 港元\n"
            f"20日涨跌幅：{status['momentum_20d_pct']:+.2f}%（阈值 ±{status['threshold_pct']:.0f}%）\n"
            f"动作：下一交易日开盘前人工复核；系统不会下单。{borrow}")
    return fp, text

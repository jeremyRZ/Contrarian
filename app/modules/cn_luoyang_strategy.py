"""洛阳钼业（SH.603993）日线趋势突破策略。

所有指标只使用当日及以前的数据；收盘后生成信号，下一交易日开盘执行。
该模块只产生研究/风控建议，不提交订单。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

import pandas as pd


@dataclass(frozen=True)
class LuoyangParams:
    entry_days: int = 55
    exit_days: int = 20
    atr_multiple: float = 3.0
    hard_stop_pct: float = 0.10
    fast_ma: int = 50
    regime_ma: int = 200


def prepare_bars(frame: pd.DataFrame, params: LuoyangParams = LuoyangParams()) -> pd.DataFrame:
    """Normalize Futu daily bars and append point-in-time indicators."""
    required = {"time_key", "open", "high", "low", "close", "volume"}
    missing = required.difference(frame.columns)
    if missing:
        raise ValueError(f"日线缺少字段: {', '.join(sorted(missing))}")
    bars = frame.copy().sort_values("time_key").drop_duplicates("time_key").reset_index(drop=True)
    bars["time_key"] = pd.to_datetime(bars["time_key"])
    previous_close = bars["close"].shift(1)
    true_range = pd.concat(
        [bars["high"] - bars["low"],
         (bars["high"] - previous_close).abs(),
         (bars["low"] - previous_close).abs()], axis=1).max(axis=1)
    bars["atr20"] = true_range.rolling(20).mean()
    bars["ma_fast"] = bars["close"].rolling(params.fast_ma).mean()
    bars["ma_regime"] = bars["close"].rolling(params.regime_ma).mean()
    # shift(1) is essential: today's breakout/exit level cannot include today's bar.
    bars["entry_level"] = bars["high"].shift(1).rolling(params.entry_days).max()
    bars["exit_level"] = bars["close"].shift(1).rolling(params.exit_days).min()
    return bars


def close_signal(row: pd.Series, holding: bool, entry_price: float | None = None,
                 peak_price: float | None = None,
                 params: LuoyangParams = LuoyangParams()) -> dict:
    """Return the action to execute at the next tradable open."""
    needed = ("close", "ma_fast", "ma_regime", "entry_level", "exit_level", "atr20")
    if any(pd.isna(row.get(key)) for key in needed):
        return {"action": "WAIT", "reason": "历史数据不足", "execute": "不操作"}

    close = float(row["close"])
    if not holding:
        trend_ok = close > float(row["ma_regime"]) and float(row["ma_fast"]) > float(row["ma_regime"])
        if trend_ok and close > float(row["entry_level"]):
            return {
                "action": "BUY",
                "reason": f"收盘突破前{params.entry_days}日高点，且MA{params.fast_ma}>MA{params.regime_ma}",
                "execute": "下一交易日开盘买入目标仓位",
            }
        return {"action": "WAIT", "reason": "未同时满足突破与长期趋势过滤", "execute": "不追涨"}

    if entry_price is None or peak_price is None:
        raise ValueError("持仓状态必须提供 entry_price 和 peak_price")
    trailing_stop = float(peak_price) - params.atr_multiple * float(row["atr20"])
    hard_stop = float(entry_price) * (1 - params.hard_stop_pct)
    active_stop = max(trailing_stop, hard_stop)
    reasons = []
    if close < active_stop:
        reasons.append(f"收盘跌破保护止损 {active_stop:.2f}")
    if close < float(row["exit_level"]):
        reasons.append(f"收盘跌破前{params.exit_days}日最低收盘")
    if close < float(row["ma_regime"]):
        reasons.append(f"收盘跌破MA{params.regime_ma}")
    if reasons:
        return {"action": "SELL", "reason": "；".join(reasons), "execute": "下一交易日开盘清仓"}
    return {
        "action": "HOLD",
        "reason": f"趋势仍有效；当前保护止损 {active_stop:.2f}",
        "execute": "继续持有，不加仓",
        "active_stop": round(active_stop, 3),
    }


def latest_levels(frame: pd.DataFrame, params: LuoyangParams = LuoyangParams()) -> dict:
    bars = prepare_bars(frame, params)
    row = bars.iloc[-1]
    return {
        "code": str(row.get("code", "SH.603993")),
        "as_of": str(row["time_key"].date()),
        "close": round(float(row["close"]), 3),
        "entry_level": round(float(row["entry_level"]), 3),
        "exit_level": round(float(row["exit_level"]), 3),
        "ma_fast": round(float(row["ma_fast"]), 3),
        "ma_regime": round(float(row["ma_regime"]), 3),
        "atr20": round(float(row["atr20"]), 3),
        "params": asdict(params),
    }

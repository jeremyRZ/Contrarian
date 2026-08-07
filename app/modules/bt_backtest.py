"""
backtrader 回测引擎（M2' 全量改写）

- 用 backtrader 作为真实成交级回测引擎：逐标的跑一个 Cerebro，
  策略在 each bar 调 screener.evaluate_signals（与实盘同一套信号逻辑），
  命中买入信号（score>=推送门槛）即开多，按 Connors 规则出场（价格>5日线 /
  持有 forward_days / 单笔止损），并对每笔成交建模港股佣金、法定费用与滑点。
- 每笔成交按触发它的信号做归因，分组统计每策略的胜率/盈亏比/最差单笔回撤/收益波动比/样本数
  与可信度徽章，输出与原 backtest 报告结构兼容的 BacktestReport。
- 数据：复用 futu_client.history_kline 拉日 K，重建 OHLCV + 特征列
  (turnover 小数→百分比、hi52/lo52 滚动、sma50/sma200、RSI(2))。

模块边界：
- 信号逻辑仍由 screener.evaluate_signals 唯一提供（解耦不变）。
- 本模块只负责「执行 + 统计 + 成本建模」，不改变信号定义。
"""
from __future__ import annotations

import math
import statistics
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_CEILING, ROUND_HALF_UP
from typing import Optional

import numpy as np
import pandas as pd

import backtrader as bt
import futu as ft

from . import screener, strategy_config

# 策略 key -> evaluate_signals 产出的信号标签（回测归因与展示）
STRATEGY_LABELS = {
    "deep_drop": "深度超跌反弹",
    "vol_breakout": "放量突破",
    "low_pe_high_div": "低PE低位",
    "hstech_link": "恒科急跌联动低吸",
    "panic_drop": "异常放量急跌(逆向)",
    "leader_pool": "龙头观察池",
    "rsi2_connor": "RSI2 逆向低吸",
}
LEADER_LABEL = "龙头观察池"

# 进程级缓存
_REPORT_CACHE: dict = {}
_LAST_REPORT: Optional[dict] = None
# K 线特征帧缓存 (code, lookback) -> DataFrame
_FRAME_CACHE: dict = {}

# 预热长度：sma200 需要 ~200 根；fetch lookback = window_days + WARMUP，
# 回测只在预热之后的 window_days 根上交易，保证样本量与旧版相当。
WARMUP = 200


# ---------------------------------------------------------------------------
# 港股成本建模
# ---------------------------------------------------------------------------
class HKCommission(bt.CommissionInfo):
    """港股股票双边交易成本，按每次成交返回绝对金额。"""

    params = (
        ("rate", 0.0005),
        ("min_commission", 0.0),
        ("stamp", 0.001),
        ("sfc_levy", 0.000027),
        ("afrc_levy", 0.0000015),
        ("trading_fee", 0.0000565),
        ("settlement_fee", 0.000042),
    )

    @staticmethod
    def _cent(value: Decimal) -> Decimal:
        return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    # 注意：backtrader 的钩子是 _getcommission(size, price, pseudoexec)，
    # 重写 getcommission 会导致下单时 TypeError（内部以 pseudoexec 调用）。
    def _getcommission(self, size, price, pseudoexec):
        notional = Decimal(str(abs(size) * price))
        brokerage = max(
            self._cent(notional * Decimal(str(self.p.rate))),
            Decimal(str(self.p.min_commission)),
        )
        # 港股股票印花税买卖双方均收，逐张成交单向上取整至港元。
        stamp = (notional * Decimal(str(self.p.stamp))).quantize(
            Decimal("1"), rounding=ROUND_CEILING
        )
        statutory = sum(
            (
                self._cent(notional * Decimal(str(rate)))
                for rate in (
                    self.p.sfc_levy,
                    self.p.afrc_levy,
                    self.p.trading_fee,
                    self.p.settlement_fee,
                )
            ),
            Decimal("0"),
        )
        return float(brokerage + stamp + statutory)


# ---------------------------------------------------------------------------
# 数据接入：扩展 PandasData 以携带特征列
# ---------------------------------------------------------------------------
class FeatureData(bt.feeds.PandasData):
    params = (
        ("datetime", None),
        ("open", 0), ("high", 1), ("low", 2), ("close", 3),
        ("volume", 4), ("openinterest", -1),
        ("turnover", 5), ("pe", 6), ("hi52", 7), ("lo52", 8),
        ("pos_pct", 9), ("sma50", 10), ("sma200", 11), ("rsi2", 12),
        ("hstech_crash", 13),
    )
    lines = ("turnover", "pe", "hi52", "lo52", "pos_pct",
             "sma50", "sma200", "rsi2", "hstech_crash")


def _num(v):
    try:
        if v is None:
            return None
        if isinstance(v, (bytes, bytearray)):  # futu 部分字段返回 numpy bytes
            v = v.decode("utf-8", "ignore")
        if isinstance(v, str):
            vs = v.strip()
            if vs.upper() in ("N/A", "NA", ""):
                return None
            return float(vs)
        f = float(v)
        if math.isnan(f):
            return None
        return f
    except (ValueError, TypeError):
        return None


def _rsi(close: pd.Series, period: int = 2) -> pd.Series:
    """Wilder 平滑 RSI（默认 2 期 = Connors RSI(2)）。"""
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    rsi = 100.0 - 100.0 / (1.0 + rs)
    return rsi


def _fetch_kline(client, code: str, window_days: int):
    """拉日 K，返回 (DataFrame[open,high,low,close,last_close,volume,turnover,pe],
    error)。索引为日期字符串列表（另返回）。"""
    end = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=int(window_days * 1.5))).strftime("%Y-%m-%d")
    df, err = client.history_kline(code, ft.KLType.K_DAY, max_count=window_days,
                                   start=start, end=end)
    if err or df is None or df.empty:
        return None, (err or "空数据"), []
    cols = {c.lower(): c for c in df.columns}
    out = pd.DataFrame()
    out["open"] = df[cols["open"]].map(_num) if "open" in cols else np.nan
    out["high"] = df[cols["high"]].map(_num) if "high" in cols else np.nan
    out["low"] = df[cols["low"]].map(_num) if "low" in cols else np.nan
    out["close"] = df[cols["close"]].map(_num) if "close" in cols else np.nan
    out["last_close"] = df[cols["last_close"]].map(_num) if "last_close" in cols else np.nan
    out["volume"] = df[cols["volume"]].map(_num) if "volume" in cols else 0.0
    out["turnover"] = df[cols["turnover_rate"]].map(_num) if "turnover_rate" in cols else 0.0
    out["pe"] = df[cols["pe_ratio"]].map(_num) if "pe_ratio" in cols else np.nan
    tk = df[cols["time_key"]] if "time_key" in cols else None
    out = out.reset_index(drop=True)
    dates = [str(x)[:10] for x in (tk.tolist() if tk is not None else [])]
    return out, None, dates


def build_feature_frame(client, code: str, lookback: int, crash_map: Optional[dict] = None):
    """拉 K 并重建特征帧（DataFrame，datetime 索引）。返回 (frame|None, error)。

    列顺序固定：[open,high,low,close,volume,turnover,pe,hi52,lo52,
    pos_pct,sma50,sma200,rsi2,hstech_crash]，供 FeatureData 按位置映射。
    turnover 小数→百分比，对齐实盘快照量程；hi52/lo52 取 lookback 内滚动极值。
    """
    # 每个自然日重新拉取，避免进程常驻后永远使用旧 K 线。恒科 crash_map 不进入
    # 基础缓存，而是在返回副本时注入，避免阈值变化复用旧特征。
    key = (code, lookback, datetime.now().strftime("%Y-%m-%d"))
    if key in _FRAME_CACHE:
        base = _FRAME_CACHE[key]
        out = base.copy()
        if crash_map:
            out["hstech_crash"] = [bool(crash_map.get(d.strftime("%Y-%m-%d"), False))
                                     for d in out.index]
        return out, None
    df, err, dates = _fetch_kline(client, code, lookback)
    if err or df is None or df.empty:
        return None, err
    raw_rows = len(df)
    # 统一索引为交易日：df 原为 RangeIndex，若直接赋值给 DatetimeIndex 的新表
    # 会被 pandas 按标签对齐成全 NaN，必须先把索引换成日期。
    if dates and len(dates) == raw_rows:
        df.index = pd.to_datetime(dates)
    else:
        return None, f"K线{raw_rows}根但日期缺失({len(dates)})"
    close = df["close"]
    look = min(lookback, 250)
    hi52, lo52 = _rolling_52week_range(df, look)
    sma50 = close.rolling(50, min_periods=50).mean()
    sma200 = close.rolling(200, min_periods=200).mean()
    rsi2 = _rsi(close, 2)
    pos = (close - lo52) / (hi52 - lo52) * 100
    pos = pos.where(hi52 > lo52, other=np.nan)

    out = pd.DataFrame(index=df.index)
    out["open"] = df["open"]
    out["high"] = df["high"]
    out["low"] = df["low"]
    out["close"] = close
    out["volume"] = df["volume"]
    out["turnover"] = df["turnover"] * 100.0  # 小数→百分比
    out["pe"] = df["pe"]
    out["hi52"] = hi52
    out["lo52"] = lo52
    out["pos_pct"] = pos
    out["sma50"] = sma50
    out["sma200"] = sma200
    out["rsi2"] = rsi2
    out["hstech_crash"] = False
    out = out[out["close"].notna()].sort_index()
    if out.empty:
        return None, f"K线{raw_rows}根但收盘价全为空(字段解析失败)"
    _FRAME_CACHE[key] = out
    if crash_map:
        out = out.copy()
        out["hstech_crash"] = [bool(crash_map.get(d.strftime("%Y-%m-%d"), False))
                                 for d in out.index]
    return out, None


def _rolling_52week_range(df: pd.DataFrame, lookback: int = 250):
    """Use actual intraday highs/lows; close-only ranges understate drawdowns."""
    look = min(int(lookback), 250)
    return (
        df["high"].rolling(look, min_periods=20).max(),
        df["low"].rolling(look, min_periods=20).min(),
    )


def latest_trend(client, code: str, window: int = 250) -> dict:
    """返回该标的最新 sma50/sma200/rsi2（供实盘 screen 使用）。"""
    frame, err = build_feature_frame(client, code, window + WARMUP, None)
    if err or frame is None or frame.empty:
        return {}
    last = frame.iloc[-1]

    def _f(v):
        return None if (v is None or (isinstance(v, float) and math.isnan(v))) else float(v)
    return {"sma50": _f(last["sma50"]), "sma200": _f(last["sma200"]), "rsi2": _f(last["rsi2"])}


def _hstech_crash_map(client, hstech_code: str, lookback: int, threshold: float) -> dict:
    """返回 {日期: 是否急跌(crash)}，用于回测中恒科联动信号逐日判定。"""
    frame, err = build_feature_frame(client, hstech_code, lookback, None)
    if err or frame is None or frame.empty:
        return {}
    chg = frame["close"].pct_change() * 100
    return {d.strftime("%Y-%m-%d"): (bool(c <= threshold) if c == c else False)
            for d, c in chg.items()}


# ---------------------------------------------------------------------------
# 回测策略
# ---------------------------------------------------------------------------
class ContrarianBacktestStrategy(bt.Strategy):
    """两种入场口径：

    - mode="combo"：综合分 >= 推送门槛才建仓（等价实盘推送口径，用于 overall）。
    - mode="single"：目标信号出现即建仓（用于单策略胜率统计，避免弱权重信号
      因分数不够门槛而永远无样本）。
    """

    params = (("cfg", None), ("forward_days", 10), ("stop_pct", 0.08),
              ("is_leader", False), ("warmup", WARMUP), ("push_threshold", 6.0),
              ("mode", "combo"), ("target_label", None))

    def __init__(self):
        self.sma5 = bt.indicators.SMA(self.data.close, period=5)
        self.trades: list = []
        self.order = None
        self.entry_signals: list = []
        self.entry_price = None
        self.entry_size = 0.0
        self.bars_held = 0
        self.peak = 0.0
        self.max_dd = 0.0
        self.bars_scanned = 0
        self.signal_hits = 0
        self.sig_freq: dict = {}
        self.max_score = 0.0
        self.order_status: dict = {}
        self.equity_curve: list = []

    def _record_equity(self) -> None:
        if not len(self.data):
            return
        day = self.data.datetime.date(0).isoformat()
        point = (day, float(self.broker.getvalue()))
        if self.equity_curve and self.equity_curve[-1][0] == day:
            self.equity_curve[-1] = point
        else:
            self.equity_curve.append(point)

    def prenext(self):
        """指标预热期不属于策略观察窗口，不纳入组合收益统计。"""
        pass

    def _feats(self) -> dict:
        price = self.data.close[0]
        prev = self.data.close[-1] if len(self.data.close) > 1 else price
        chg = (price - prev) / prev * 100 if prev else 0.0
        prevc = prev if prev else price
        high = self.data.high[0]
        low = self.data.low[0]
        amp = (high - low) / prevc * 100 if prevc else None
        turn = self.data.turnover[0]
        turn = 0.0 if (turn != turn) else float(turn)  # NaN→0
        pe = self.data.pe[0]
        pe = None if (pe != pe) else float(pe)
        sma50 = self.data.sma50[0]
        sma200 = self.data.sma200[0]
        rsi2 = self.data.rsi2[0]
        return {
            "price": float(price),
            "change_rate": float(chg),
            "prev_close_price": float(prev),
            "turnover_rate": turn,
            "amplitude": (float(amp) if amp is not None else None),
            "pe": pe,
            "hi52": (float(self.data.hi52[0]) if self.data.hi52[0] == self.data.hi52[0] else None),
            "lo52": (float(self.data.lo52[0]) if self.data.lo52[0] == self.data.lo52[0] else None),
            "pos_pct": (float(self.data.pos_pct[0]) if self.data.pos_pct[0] == self.data.pos_pct[0] else None),
            "is_leader": self.p.is_leader,
            "hstech_crash": bool(self.data.hstech_crash[0]),
            "sma50": (float(sma50) if sma50 == sma50 else None),
            "sma200": (float(sma200) if sma200 == sma200 else None),
            "rsi2": (float(rsi2) if rsi2 == rsi2 else None),
        }

    def next(self):
        if len(self) < self.p.warmup:  # 预热：等均线有效
            return
        self._record_equity()
        if self.order:
            return
        self.bars_scanned += 1
        f = self._feats()
        ev = screener.evaluate_signals(f, self.p.cfg)
        for _s in ev["signals"]:
            self.sig_freq[_s] = self.sig_freq.get(_s, 0) + 1
        if ev["score"] > self.max_score:
            self.max_score = ev["score"]
        if self.p.mode == "single":
            fire = self.p.target_label in ev["signals"]
        else:
            fire = ev["score"] >= self.p.push_threshold and bool(ev["signals"])
        if fire:
            self.signal_hits += 1
        if not self.position:
            # 仅记录真实信号（剔除龙头标签）
            if fire:
                self.order = self.buy()
                self.entry_signals = [s for s in ev["signals"] if s != LEADER_LABEL]
                self.entry_price = self.data.close[0]
                self.bars_held = 0
                self.peak = self.data.close[0]
                self.max_dd = 0.0
        else:
            self.bars_held += 1
            price = self.data.close[0]
            if price > self.peak:
                self.peak = price
            if self.peak > 0:
                dd = (self.peak - price) / self.peak * 100
                if dd > self.max_dd:
                    self.max_dd = dd
            stop_hit = price <= self.entry_price * (1.0 - self.p.stop_pct)
            rebound = self.bars_held >= 1 and price > self.sma5[0]
            timeout = self.bars_held >= self.p.forward_days
            if stop_hit or rebound or timeout:
                self.order = self.sell()

    def notify_order(self, order):
        """订单终态必须清空 self.order，否则 next() 会被永久挡住（无法卖出）。"""
        if order.status in (order.Submitted, order.Accepted):
            return
        _st = order.getstatusname()
        self.order_status[_st] = self.order_status.get(_st, 0) + 1
        if order.status == order.Completed and order.isbuy():
            # 以实际成交价（次日开盘）为基准，止损/回撤统计才真实
            self.entry_price = float(order.executed.price)
            self.entry_size = abs(float(order.executed.size))
            self.peak = float(order.executed.price)
            self.bars_held = 0
            self.max_dd = 0.0
        self.order = None

    def notify_trade(self, trade):
        if trade.isclosed:
            # trade.size 在平仓时已归零，必须用建仓成交记录算名义本金
            notional = (self.entry_size * self.entry_price
                        if (self.entry_size and self.entry_price) else 0.0)
            net_ret = (trade.pnlcomm / notional * 100.0) if notional else 0.0
            self.trades.append({
                "signals": list(self.entry_signals),
                "ret": float(net_ret),
                "dd": float(self.max_dd),
            })
            self.entry_signals = []
            self.max_dd = 0.0


# ---------------------------------------------------------------------------
# 统计
# ---------------------------------------------------------------------------
def _aggregate_equal_weight_curves(
        curves: dict[str, pd.Series], initial_capital: float = 100000.0,
        sleeve_initial_capital: float = 100000.0) -> pd.Series:
    """把独立单票资金袖套聚合为固定等权组合净值。"""
    clean = []
    for series in curves.values():
        if series is None or series.empty:
            continue
        s = pd.to_numeric(series, errors="coerce").dropna().sort_index()
        s = s[~s.index.duplicated(keep="last")]
        if not s.empty:
            clean.append(s / float(sleeve_initial_capital))
    if not clean:
        return pd.Series(dtype=float)
    normalized = pd.concat(clean, axis=1).sort_index()
    # 尚未开始交易的袖套按现金净值 1.0 处理；上市后的休市日沿用上一净值。
    normalized = normalized.ffill().fillna(1.0)
    return normalized.mean(axis=1) * float(initial_capital)


def _portfolio_metrics(equity: pd.Series, risk_free_rate: float = 0.0) -> dict:
    """从日频组合净值计算真实组合风险指标；百分比字段以 0-100 返回。"""
    values = pd.to_numeric(equity, errors="coerce").dropna().sort_index()
    values = values[~values.index.duplicated(keep="last")]
    returns = values.pct_change().dropna()
    if len(values) < 2 or returns.empty or values.iloc[0] <= 0:
        return {
            "metric_scope": "portfolio_daily", "observations": 0,
            "total_return": None, "annual_return": None,
            "annual_volatility": None, "max_drawdown": None,
            "sharpe": None, "sortino": None, "calmar": None,
            "var_95": None, "cvar_95": None, "max_drawdown_duration": 0,
        }

    ann_factor = 252.0
    total_return = float(values.iloc[-1] / values.iloc[0] - 1.0)
    growth = max(float(values.iloc[-1] / values.iloc[0]), 0.0)
    annual_return = growth ** (ann_factor / len(returns)) - 1.0 if growth > 0 else -1.0
    daily_vol = float(returns.std(ddof=1)) if len(returns) > 1 else 0.0
    annual_vol = daily_vol * math.sqrt(ann_factor)
    annual_excess = float(returns.mean()) * ann_factor - float(risk_free_rate)
    sharpe = annual_excess / annual_vol if annual_vol > 0 else None

    downside = np.minimum(returns.to_numpy(dtype=float), 0.0)
    downside_dev = float(np.sqrt(np.mean(np.square(downside))) * math.sqrt(ann_factor))
    sortino = annual_excess / downside_dev if downside_dev > 0 else None

    drawdowns = values / values.cummax() - 1.0
    max_drawdown = abs(float(drawdowns.min()))
    calmar = annual_return / max_drawdown if max_drawdown > 0 else None

    var_cutoff = float(np.percentile(returns.to_numpy(dtype=float), 5.0))
    tail = returns[returns <= var_cutoff]
    var_95 = max(0.0, -var_cutoff)
    cvar_95 = max(0.0, -float(tail.mean())) if not tail.empty else var_95

    max_duration = current_duration = 0
    for in_drawdown in (drawdowns < 0):
        current_duration = current_duration + 1 if in_drawdown else 0
        max_duration = max(max_duration, current_duration)

    def _pct(value: float) -> float:
        return round(value * 100.0, 2)

    return {
        "metric_scope": "portfolio_daily",
        "observations": int(len(returns)),
        "total_return": _pct(total_return),
        "annual_return": _pct(annual_return),
        "annual_volatility": _pct(annual_vol),
        "max_drawdown": _pct(max_drawdown),
        "sharpe": (round(sharpe, 3) if sharpe is not None else None),
        "sortino": (round(sortino, 3) if sortino is not None else None),
        "calmar": (round(calmar, 3) if calmar is not None else None),
        "var_95": _pct(var_95),
        "cvar_95": _pct(cvar_95),
        "max_drawdown_duration": int(max_duration),
    }


def _temporal_validation(equity: pd.Series, risk_free_rate: float = 0.0,
                         train_ratio: float = 0.8, rolling_days: int = 63) -> dict:
    """Chronological holdout and rolling stability diagnostics without future leakage."""
    values = pd.to_numeric(equity, errors="coerce").dropna().sort_index()
    if len(values) < 10:
        return {
            "method": "chronological_holdout",
            "status": "insufficient_data",
            "is_pristine_oos": False,
            "limitations": ["样本不足", "当前参数曾由历史网格选择，不能宣称为纯样本外结果"],
        }
    split = min(len(values) - 2, max(2, int(len(values) * train_ratio)))
    train = values.iloc[:split]
    # Include the boundary equity so the first holdout daily return is retained.
    test = values.iloc[split - 1:]
    windows = []
    if len(values) >= rolling_days:
        step = max(1, rolling_days // 3)
        for start in range(0, len(values) - rolling_days + 1, step):
            segment = values.iloc[start:start + rolling_days]
            metrics = _portfolio_metrics(segment, risk_free_rate)
            windows.append({
                "start": segment.index[0].strftime("%Y-%m-%d"),
                "end": segment.index[-1].strftime("%Y-%m-%d"),
                "total_return": metrics.get("total_return"),
                "max_drawdown": metrics.get("max_drawdown"),
                "sharpe": metrics.get("sharpe"),
            })
    positive = sum(1 for item in windows if (item.get("total_return") or 0) > 0)
    return {
        "method": "chronological_holdout",
        "status": "diagnostic",
        "split_date": values.index[split].strftime("%Y-%m-%d"),
        "train": _portfolio_metrics(train, risk_free_rate),
        "holdout": _portfolio_metrics(test, risk_free_rate),
        "rolling_days": rolling_days,
        "rolling_windows": windows,
        "positive_window_rate": (
            round(positive / len(windows) * 100, 1) if windows else None
        ),
        "is_pristine_oos": False,
        "limitations": [
            "当前参数曾由历史网格选择，留出段仅用于时间稳定性诊断",
            "股票池为当前观察池，仍存在幸存者偏差",
            "反向数据源没有可靠历史快照，未纳入回测",
        ],
    }


def _stats(returns: list, draws: list) -> dict:
    n = len(returns)
    if n == 0:
        return {"n": 0, "win_rate": None, "avg_ret": None, "profit_factor": None,
                "payoff": None, "max_drawdown": None, "sharpe": None,
                "max_adverse_excursion": None, "trade_return_ratio": None,
                "metric_scope": "trade",
                "confidence": "样本不足(无统计意义)", "note": "无信号样本"}
    wins = [r for r in returns if r > 0]
    losses = [r for r in returns if r <= 0]
    win_rate = round(len(wins) / n * 100, 1)
    avg_ret = round(statistics.mean(returns), 2)
    gains = statistics.mean(wins) if wins else 0.0
    loss_abs = statistics.mean([-x for x in losses]) if losses else 0.0
    # 赔率（平均盈利/平均亏损）与盈利因子（总盈利/总亏损）是两回事，分开给
    payoff = round(gains / loss_abs, 2) if loss_abs > 0 else None
    sum_win = sum(wins)
    sum_loss = sum(-x for x in losses)
    pf = round(sum_win / sum_loss, 2) if sum_loss > 0 else None
    worst_trade_dd = round(max(draws), 2) if draws else None
    std = statistics.stdev(returns) if n > 1 else 0.0
    trade_return_ratio = round(avg_ret / std, 2) if std > 0 else None
    conf = ("相对可信" if n >= 20 else "弱可信") if n >= 8 else "样本不足(无统计意义)"
    return {"n": n, "win_rate": win_rate, "avg_ret": avg_ret,
            "profit_factor": pf, "payoff": payoff,
            # 当前引擎逐标的独立运行，无法从聚合交易列表重建组合资金曲线，
            # 因此不伪造组合最大回撤或年化夏普；只报告可由交易样本直接计算的指标。
            "max_drawdown": None, "sharpe": None,
            "max_adverse_excursion": worst_trade_dd,
            "trade_return_ratio": trade_return_ratio,
            "metric_scope": "trade", "confidence": conf}


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def run_backtest(codes: Optional[list] = None, cfg: Optional[dict] = None,
                 client=None, window_days: int = 250, forward_days: int = 10,
                 hstech_code: str = "HK.800700", no_fee: bool = False) -> dict:
    """运行回测，返回 BacktestReport（结构与原 backtest 报告兼容）。"""
    from .. import futu_client
    if client is None:
        client = futu_client.build_client_from_config()
    cfg = cfg or strategy_config.load_config()
    codes = codes or screener.LEADERS
    bt_cfg = cfg.get("backtest", {})
    fwd = forward_days or int(bt_cfg.get("forward_days", 10))
    stop = float(bt_cfg.get("stop_pct", 0.08))
    def _cost(name: str, default: float) -> float:
        return 0.0 if no_fee else float(bt_cfg.get(name, default))

    comm = _cost("commission", 0.0005)
    min_commission = _cost("min_commission", 0.0)
    stamp = _cost("stamp", 0.001)
    sfc_levy = _cost("sfc_levy", 0.000027)
    afrc_levy = _cost("afrc_levy", 0.0000015)
    trading_fee = _cost("trading_fee", 0.0000565)
    settlement_fee = _cost("settlement_fee", 0.000042)
    slippage = _cost("slippage", 0.0005)
    risk_free_rate = float(bt_cfg.get("risk_free_rate", 0.0))
    exec_on_close = bool(bt_cfg.get("exec_on_close", False))
    push_th = cfg["push"]["light"]
    hstech_th = cfg["strategies"]["hstech_link"]["hstech_drop"]
    lookback = window_days + WARMUP

    crash_map = _hstech_crash_map(client, hstech_code, lookback, hstech_th)

    per_signal = {lab: {"returns": [], "draws": []} for lab in STRATEGY_LABELS.values()}
    all_ret, all_dd = [], []
    per_code = {}
    portfolio_curves: dict[str, pd.Series] = {}

    def _run_one(frame, code, mode, target_label=None):
        """跑一个 Cerebro，返回 strategy 实例（失败返回 None）。"""
        cerebro = bt.Cerebro(stdstats=False)
        cerebro.adddata(FeatureData(dataname=frame))
        cerebro.broker.setcash(100000.0)
        cerebro.broker.addcommissioninfo(HKCommission(
            rate=comm,
            min_commission=min_commission,
            stamp=stamp,
            sfc_levy=sfc_levy,
            afrc_levy=afrc_levy,
            trading_fee=trading_fee,
            settlement_fee=settlement_fee,
        ))
        if slippage > 0:
            cerebro.broker.set_slippage_perc(
                slippage, slip_open=True, slip_limit=True,
                slip_match=True, slip_out=False,
            )
        # 默认在下一根 K 线开盘成交，避免使用尚未形成的当日收盘信号成交。
        # exec_on_close 仅保留为显式的乐观情景模拟开关。
        if exec_on_close:
            cerebro.broker.set_coc(True)
        # 90% 仓位：留出佣金/印花税缓冲，避免保证金不足被拒单
        cerebro.addsizer(bt.sizers.PercentSizer, percents=90)
        cerebro.addstrategy(
            ContrarianBacktestStrategy,
            cfg=cfg, forward_days=fwd, stop_pct=stop,
            is_leader=(code in screener.LEADERS),
            warmup=WARMUP, push_threshold=push_th,
            mode=mode, target_label=target_label,
        )
        return cerebro.run()[0]

    # 需要单独统计胜率的策略标签（仅启用的入场信号；龙头观察池是标签不是入场信号）
    active_labels = [lab for key, lab in STRATEGY_LABELS.items()
                     if cfg["strategies"].get(key, {}).get("enabled", True)
                     and lab != LEADER_LABEL]

    skipped = {}
    bars_total = 0
    sig_freq_all: dict = {}
    max_score_all = 0.0
    order_stats: dict = {}
    for code in codes:
        frame, err = build_feature_frame(client, code, lookback, crash_map)
        if err or frame is None:
            skipped[code] = err or "无数据"
            continue
        if len(frame) < WARMUP + 5:
            skipped[code] = f"K线不足({len(frame)}根 < 预热{WARMUP}+5)"
            continue
        # 1) 组合口径（等价实盘推送）→ overall / per_code
        try:
            strat = _run_one(frame, code, "combo")
        except Exception as exc:  # noqa: BLE001
            skipped[code] = f"回测异常: {type(exc).__name__}: {exc}"
            continue
        if strat.equity_curve:
            dates = pd.to_datetime([point[0] for point in strat.equity_curve])
            values = [point[1] for point in strat.equity_curve]
            portfolio_curves[code] = pd.Series(values, index=dates, dtype=float)
        bars_total += strat.bars_scanned
        for _s, _c in strat.sig_freq.items():
            sig_freq_all[_s] = sig_freq_all.get(_s, 0) + _c
        if strat.max_score > max_score_all:
            max_score_all = strat.max_score
        code_rets = [t["ret"] for t in strat.trades]
        for t in strat.trades:
            all_ret.append(t["ret"])
            all_dd.append(t["dd"])
        if code_rets:
            per_code[code] = {"entries": len(code_rets),
                              "avg_ret": round(statistics.mean(code_rets), 2)}
        # 2) 单策略口径 → per_strategy（每个信号独立成交统计）
        for lab in active_labels:
            try:
                s1 = _run_one(frame, code, "single", lab)
            except Exception as exc:  # noqa: BLE001
                skipped[f"{code}|{lab}"] = f"{type(exc).__name__}: {exc}"
                continue
            for _st, _c in s1.order_status.items():
                order_stats[_st] = order_stats.get(_st, 0) + _c
            if s1.signal_hits and not s1.trades:
                order_stats[f"未成交:{lab}(命中{s1.signal_hits})"] = \
                    order_stats.get(f"未成交:{lab}(命中{s1.signal_hits})", 0) + 1
            for t in s1.trades:
                per_signal[lab]["returns"].append(t["ret"])
                per_signal[lab]["draws"].append(t["dd"])

    per_strategy = {}
    for key, lab in STRATEGY_LABELS.items():
        blk = cfg["strategies"].get(key, {})
        per_strategy[lab] = {
            **_stats(per_signal[lab]["returns"], per_signal[lab]["draws"]),
            "enabled": blk.get("enabled", True),
            "weight": blk.get("weight", 0.0),
        }
        if lab == LEADER_LABEL:
            per_strategy[lab]["note"] = "标签类（非入场信号，不单独统计）"
            per_strategy[lab]["confidence"] = "—"
    overall = _stats(all_ret, all_dd)
    portfolio_equity = _aggregate_equal_weight_curves(portfolio_curves)
    portfolio = {
        "model": "equal_weight_sleeves",
        "initial_capital": 100000.0,
        "sleeves": len(portfolio_curves),
        **_portfolio_metrics(portfolio_equity, risk_free_rate=risk_free_rate),
        "equity_curve": [
            {"date": index.strftime("%Y-%m-%d"), "equity": round(float(value), 2)}
            for index, value in portfolio_equity.items()
        ],
        "validation": _temporal_validation(portfolio_equity, risk_free_rate),
    }

    return {
        "per_strategy": per_strategy,
        "overall": overall,
        "portfolio": portfolio,
        "backtest_scope": {
            "signal_scope": "technical_only",
            "production_trigger_covered": "technical_backtested",
            "reverse_history_included": False,
            "quality_gate_history_included": False,
            "note": "反向/错价证据没有点时历史快照，只能标为未验证研究信号，不能借用技术回测背书。",
        },
        "per_code": per_code,
        "window_days": window_days,
        "forward_days": fwd,
        "push_threshold": push_th,
        "hstech_code": hstech_code,
        "codes_count": len(codes),
        "codes_used": len(portfolio_curves),
        "bars_tested": bars_total,
        "signal_freq": sig_freq_all,
        "order_stats": order_stats,
        "max_score": max_score_all,
        "skipped": skipped,
        "with_cost": (not no_fee),
        "cost_model": {
            "brokerage_rate": comm,
            "minimum_brokerage": min_commission,
            "stamp_duty_rate": stamp,
            "stamp_scope": "both_sides",
            "sfc_levy_rate": sfc_levy,
            "afrc_levy_rate": afrc_levy,
            "trading_fee_rate": trading_fee,
            "settlement_fee_rate": settlement_fee,
            "slippage_rate": slippage,
        },
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def debug_signals(code: str, cfg: Optional[dict] = None, client=None,
                  window_days: int = 250, hstech_code: str = "HK.800700") -> dict:
    """诊断：在特征帧上逐根 bar 跑 evaluate_signals，统计各信号触发频次。

    不经过 backtrader，用于快速定位「回测无成交」是信号没触发还是执行层问题。
    """
    from .. import futu_client
    if client is None:
        client = futu_client.build_client_from_config()
    cfg = cfg or strategy_config.load_config()
    lookback = window_days + WARMUP
    hstech_th = cfg["strategies"]["hstech_link"]["hstech_drop"]
    crash_map = _hstech_crash_map(client, hstech_code, lookback, hstech_th)
    frame, err = build_feature_frame(client, code, lookback, crash_map)
    if err or frame is None:
        return {"code": code, "error": err or "无数据"}

    freq: dict = {}
    score_hist: dict = {}
    rows = 0
    sample = None
    prev_close = None
    for ts, r in frame.iterrows():
        rows += 1
        if rows <= WARMUP:
            prev_close = r["close"]
            continue
        price = float(r["close"])
        prev = float(prev_close) if prev_close is not None else price
        prev_close = r["close"]

        def _v(x):
            return None if (x is None or x != x) else float(x)
        f = {
            "price": price,
            "change_rate": ((price - prev) / prev * 100) if prev else 0.0,
            "prev_close_price": prev,
            "turnover_rate": (_v(r["turnover"]) or 0.0),
            "amplitude": (((float(r["high"]) - float(r["low"])) / prev * 100) if prev else None),
            "pe": _v(r["pe"]),
            "hi52": _v(r["hi52"]), "lo52": _v(r["lo52"]),
            "pos_pct": _v(r["pos_pct"]),
            "is_leader": code in screener.LEADERS,
            "hstech_crash": bool(r["hstech_crash"]),
            "sma50": _v(r["sma50"]), "sma200": _v(r["sma200"]), "rsi2": _v(r["rsi2"]),
        }
        ev = screener.evaluate_signals(f, cfg)
        for s in ev["signals"]:
            freq[s] = freq.get(s, 0) + 1
        sc = ev["score"]
        score_hist[sc] = score_hist.get(sc, 0) + 1
        if sample is None:
            sample = {"date": str(ts)[:10], **f, "score": sc, "signals": ev["signals"]}
    return {
        "code": code, "frame_rows": len(frame), "bars_evaluated": max(0, rows - WARMUP),
        "signal_freq": freq,
        "score_hist": {str(k): v for k, v in sorted(score_hist.items(), reverse=True)},
        "push_threshold": cfg["push"]["light"],
        "first_bar_sample": sample,
    }


def sweep(codes=None, cfg: Optional[dict] = None, client=None,
          window_days: int = 250, hstech_code: str = "HK.800700",
          forward_list=(5, 10, 20), stop_list=(0.04, 0.08),
          rsi2_list=(5, 10), focus: str = "RSI2 逆向低吸") -> dict:
    """参数寻优：在 持有天数 × 止损 × RSI2阈值 网格上跑回测，返回对比表。

    K 线特征帧有进程缓存，首轮之后每组只有纯计算开销。
    返回按「期望值(平均收益)」降序排列的组合列表。
    """
    import copy
    base = cfg or strategy_config.load_config()
    rows = []
    for fwd in forward_list:
        for stop in stop_list:
            for r2 in rsi2_list:
                c = copy.deepcopy(base)
                c.setdefault("backtest", {})["forward_days"] = fwd
                c["backtest"]["stop_pct"] = stop
                c.setdefault("strategies", {}).setdefault("rsi2_connor", {})["rsi2_oversold"] = r2
                rep = run_backtest(codes, c, client, window_days, fwd, hstech_code)
                o = rep["overall"]
                f = rep["per_strategy"].get(focus, {})
                rows.append({
                    "forward_days": fwd, "stop_pct": stop, "rsi2_oversold": r2,
                    "overall_n": o["n"], "overall_win": o["win_rate"],
                    "overall_avg": o["avg_ret"], "overall_pf": o["profit_factor"],
                    "focus_n": f.get("n"), "focus_win": f.get("win_rate"),
                    "focus_avg": f.get("avg_ret"), "focus_pf": f.get("profit_factor"),
                    "focus_payoff": f.get("payoff"),
                })
    rows.sort(key=lambda x: (x["focus_avg"] is not None, x["focus_avg"] or -99), reverse=True)
    return {"focus": focus, "window_days": window_days, "rows": rows,
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}


def cached_report(codes, cfg, client, window_days, forward_days, hstech_code,
                  no_fee: bool = False) -> dict:
    """带进程缓存的回测；相同参数 1 小时内直接返回。"""
    import hashlib, json
    sig = hashlib.md5(json.dumps({
        "codes": codes, "wd": window_days, "fd": forward_days,
        "hs": hstech_code, "cfg": cfg, "nofee": no_fee,
    }, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    global _LAST_REPORT
    hit = _REPORT_CACHE.get(sig)
    if hit:
        if codes is None or len(codes) >= 20:
            _LAST_REPORT = hit
        return hit
    rep = run_backtest(codes, cfg, client, window_days, forward_days, hstech_code, no_fee)
    _REPORT_CACHE[sig] = rep
    if codes is None or len(codes) >= 20:
        _LAST_REPORT = rep
    return rep


def clear_caches(include_frames: bool = True) -> None:
    """集中清理回测报告和最近报告；强制刷新时同时清理日 K 特征。"""
    global _LAST_REPORT
    _REPORT_CACHE.clear()
    _LAST_REPORT = None
    if include_frames:
        _FRAME_CACHE.clear()


def get_cached_backtest_stats() -> Optional[dict]:
    """返回最近一次缓存回测的 per_strategy 统计 {策略名: {win_rate, profit_factor}}。

    供推送「回测背书」使用：优先用热缓存（/backtest/report 跑过即热），
    无缓存时返回 None（调用方回退到静态表）。
    """
    if not _LAST_REPORT:
        return None
    rep = _LAST_REPORT
    ps = rep.get("per_strategy") or {}
    out = {}
    for name, blk in ps.items():
        if not isinstance(blk, dict):
            continue
        out[name] = {
            "win_rate": blk.get("win_rate"),
            "profit_factor": blk.get("profit_factor"),
            "n": blk.get("n"),
        }
    return out or None


def get_cached_evidence_report() -> Optional[dict]:
    """Return only a broad-universe report suitable for production evidence gates."""
    return _LAST_REPORT

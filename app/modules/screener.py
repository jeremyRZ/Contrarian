"""
选股筛选 / 买入机会扫描模块
- 基于富途观察池快照
- 6 大策略信号 + 10 分制综合评分（复刻 hk-stock-monitor scanner 思路）
- 仓位感知：根据现金比例动态调整推送门槛（轻仓6分 / 中仓7分 / 满仓停推）
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Optional

import pandas as pd

from . import reverse_signals, strategy_config
from .. import notify

# 港股龙头观察池（约 45 只，覆盖科技/金融/消费/能源/医药）
LEADERS = [
    "HK.00700", "HK.03690", "HK.01810", "HK.09988", "HK.09618", "HK.01024",
    "HK.09626", "HK.09888", "HK.09999", "HK.00992", "HK.00981", "HK.00522",
    "HK.02382", "HK.01211", "HK.00175", "HK.02333", "HK.09868", "HK.02015",
    "HK.09866", "HK.00883", "HK.00857", "HK.00386", "HK.00005", "HK.00939",
    "HK.01398", "HK.03988", "HK.01299", "HK.02318", "HK.02628", "HK.00001",
    "HK.01113", "HK.00388", "HK.01928", "HK.00027", "HK.01093", "HK.02269",
    "HK.02359", "HK.02007", "HK.02202", "HK.00941", "HK.00267", "HK.01698",
    "HK.00241", "HK.01347", "HK.02020",
]

DEFAULT_THRESHOLDS = {"light": 6.0, "mid": 7.0}


def _col(df, col):
    cols = {c.lower(): c for c in df.columns}
    return cols.get(col.lower())


def _num(v):
    """把 futu 返回的 'N/A' / NaN / 字符串 安全转成 float，无法转换返回 None。"""
    try:
        if v is None:
            return None
        if isinstance(v, str):
            if v.strip().upper() in ("N/A", "NA", ""):
                return None
            return float(v)
        f = float(v)
        if math.isnan(f):
            return None
        return f
    except (ValueError, TypeError):
        return None


def _clean_nums(rows: list) -> list:
    """把结果中的非有限 float（nan/inf，常由停牌标的除零产生）替换为 None，避免排序/序列化出错。"""
    for r in rows:
        for k, v in list(r.items()):
            if isinstance(v, float) and not math.isfinite(v):
                r[k] = None
    return rows


def _clean_nan(obj):
    """递归把任意结构中的非有限 float（nan/inf）替换为 None，避免 JSON 序列化失败。"""
    if isinstance(obj, dict):
        return {k: _clean_nan(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_clean_nan(v) for v in obj]
    if isinstance(obj, float) and not math.isfinite(obj):
        return None
    return obj


# 实盘扫描用的趋势特征缓存（sma/rsi2 由 K 线计算，避免每只股票重复拉取）
_TREND_CACHE: dict = {}  # code -> (timestamp, {sma50, sma200, rsi2})


def _latest_trend(client, code: str, window: int = 250) -> dict:
    """返回该标的最新 sma50/sma200/rsi2（由近期 K 线计算）。失败/缺失返回 {}。"""
    import time
    now = time.time()
    cached = _TREND_CACHE.get(code)
    if cached and now - cached[0] < 3600:
        return cached[1]
    try:
        from . import bt_backtest
        trend = bt_backtest.latest_trend(client, code, window)
    except Exception:  # noqa: BLE001
        trend = {}
    _TREND_CACHE[code] = (now, trend)
    return trend


def evaluate_signals(f: dict, cfg: dict) -> dict:
    """纯函数：给定单日特征快照，返回 {score, signals[]}。

    无 IO、可重放 —— 实盘扫描（screen）与历史回测（backtest）共用同一套逻辑，
    保证回测验证的是生产环境真实使用的信号。
    f 字段：price, change_rate, prev_close_price, turnover_rate, amplitude,
            pe, hi52, lo52, pos_pct, is_leader, hstech_crash,
            sma50, sma200, rsi2（后三者可选，缺省时对应信号不触发）
    cfg：来自 strategy_config 的策略参数（阈值/权重/开关）。
    """
    s = cfg.get("strategies", {})
    signals: list = []
    score = 0.0
    price = f.get("price")
    chg = f.get("change_rate") or 0.0
    turn = f.get("turnover_rate") or 0.0
    pe = f.get("pe")
    hi = f.get("hi52")
    lo = f.get("lo52")
    pos_pct = f.get("pos_pct")

    # 趋势过滤器：下跌类信号需处于上升趋势（价格>均线）才计入，避免接飞刀。
    # 这是把胜率从 ~45% 拉到 65%+ 的关键（Connors RSI(2) 实证）。
    tf = cfg.get("trend_filter", {})
    tf_on = tf.get("enabled", False)
    tf_period = int(tf.get("period", 200))
    tf_apply = set(tf.get("apply",
                    ["deep_drop", "vol_breakout", "low_pe_high_div", "hstech_link", "panic_drop"]))
    tf_sma = f.get("sma200") if tf_period >= 100 else f.get("sma50")
    tf_sma_v = None
    if tf_sma is not None and not (isinstance(tf_sma, float) and math.isnan(tf_sma)):
        tf_sma_v = tf_sma
    uptrend = (price is not None and tf_sma_v is not None and price > tf_sma_v)

    def dip_ok(key: str) -> bool:
        """该下跌类信号是否被趋势过滤器放行。"""
        return (not tf_on) or (key not in tf_apply) or uptrend

    # 1. 深度超跌反弹：距52周高点回撤 > drop_pct 且盈利
    drop_val = ((hi - price) / hi * 100) if (price and hi and hi > 0) else None
    blk = s.get("deep_drop", {})
    if blk.get("enabled", True) and dip_ok("deep_drop"):
        if price and hi and lo and hi > lo and hi > 0:
            if drop_val > blk.get("drop_pct", 25.0) and (pe is None or pe > 0):
                signals.append("深度超跌反弹")
                score += blk.get("weight", 3.0)
    # 2. 放量突破：上涨且换手/量能活跃
    blk = s.get("vol_breakout", {})
    if blk.get("enabled", True) and dip_ok("vol_breakout"):
        if chg > blk.get("chg_pct", 2.0) and turn > blk.get("turn", 1.5):
            signals.append("放量突破")
            score += blk.get("weight", 3.0)
    # 3. 低 PE 且接近 52 周低位；股息是独立数据源，不在此技术快照中冒充。
    blk = s.get("low_pe_high_div", {})
    if blk.get("enabled", True) and dip_ok("low_pe_high_div"):
        if pe and pe < blk.get("pe", 10.0) and (pos_pct is not None and pos_pct < blk.get("pos_pct", 30.0)):
            signals.append("低PE低位")
            score += blk.get("weight", 3.0)
    # 4. 恒科急跌联动低吸：指数急跌时龙头跟跌即低吸
    blk = s.get("hstech_link", {})
    if blk.get("enabled", True) and dip_ok("hstech_link"):
        if f.get("hstech_crash") and f.get("is_leader") and chg < 0:
            signals.append("恒科急跌联动低吸")
            score += blk.get("weight", 2.0)
    # 5. 异常放量急跌(逆向)：日内大跌且放量
    blk = s.get("panic_drop", {})
    if blk.get("enabled", True) and dip_ok("panic_drop"):
        if chg < blk.get("chg_pct", -5.0) and turn > blk.get("turn", 2.0):
            signals.append("异常放量急跌(逆向)")
            score += blk.get("weight", 2.0)
    # 6. 龙头观察池（标签，不参与趋势过滤，也不单独触发回测成交）
    blk = s.get("leader_pool", {})
    if blk.get("enabled", True):
        if f.get("is_leader"):
            signals.append("龙头观察池")
            score += blk.get("weight", 1.0)
    # 7. RSI(2) Connors 逆向低吸：上升趋势中短期超卖（价格>均线 且 RSI2<阈值）
    r2 = f.get("rsi2")
    blk = s.get("rsi2_connor", {})
    if blk.get("enabled", False):
        if r2 is not None and uptrend and r2 < blk.get("rsi2_oversold", 10):
            signals.append("RSI2 逆向低吸")
            score += blk.get("weight", 2.0)

    score = min(10.0, round(score, 1))
    # 信号名 → 权重（与 cfg 同步，供推送「分数来源」拆解）
    _name_w = {
        "深度超跌反弹": s.get("deep_drop", {}).get("weight", 3.0),
        "放量突破": s.get("vol_breakout", {}).get("weight", 3.0),
        "低PE低位": s.get("low_pe_high_div", {}).get("weight", 3.0),
        "恒科急跌联动低吸": s.get("hstech_link", {}).get("weight", 2.0),
        "异常放量急跌(逆向)": s.get("panic_drop", {}).get("weight", 2.0),
        "龙头观察池": s.get("leader_pool", {}).get("weight", 1.0),
        "RSI2 逆向低吸": s.get("rsi2_connor", {}).get("weight", 2.0),
    }
    signal_details = [{"name": nm, "weight": _name_w.get(nm, 0.0)} for nm in signals]
    reason_inputs = {
        "drop_pct": round(drop_val, 1) if drop_val is not None else None,
        "rsi2": round(float(r2), 1) if r2 is not None else None,
        "uptrend": bool(uptrend),
        "hstech_crash": bool(f.get("hstech_crash")),
        "pe": pe,
        "pos_pct": round(float(pos_pct), 1) if pos_pct is not None else None,
        "chg": chg,
        "turn": turn,
    }
    return {"score": score, "signals": signals,
            "signal_details": signal_details, "reason_inputs": reason_inputs}


def _apply_score_layers(row: dict, reverse: Optional[dict]) -> dict:
    """Normalize evidence to 0-10 and disclose coverage and quality limitations."""
    technical = max(0.0, min(10.0, float(row.get("score") or 0.0)))
    reverse = reverse or {}
    raw_reverse = float(reverse.get("score") or 0.0)
    mispricing = max(0.0, min(10.0, raw_reverse))
    details = reverse.get("details") or {}
    expected = ("southbound", "buyback", "news", "capital_flow", "valuation",
                "institution", "dividend", "earnings")
    errors = [key for key in expected if (details.get(key) or {}).get("error")]
    available = sum(1 for key in expected if key in details and key not in errors)
    confidence = round(available / len(expected) * 100, 1)

    reasons = []
    status = "unknown"
    valuation = details.get("valuation") or {}
    dividend = details.get("dividend") or {}
    institution = details.get("institution") or {}
    capital_flow = details.get("capital_flow") or {}
    if dividend.get("omitted"):
        status = "fail"
        reasons.append("股息停派")
    elif institution.get("score", 0) < 0 or capital_flow.get("score", 0) < -0.5:
        status = "fail"
        reasons.append("机构或主力资金持续转弱")
    elif not valuation.get("error") and (
        valuation.get("pe_percentile") is not None
        or valuation.get("pb_percentile") is not None
    ):
        status = "pass"
        reasons.append("估值与资金证据未触发质量否决")
    else:
        reasons.append("缺少足够的盈利质量/现金流数据")

    composite = round(technical * 0.55 + mispricing * 0.45, 1)
    row.update({
        "technical_score": round(technical, 1),
        "mispricing_score": round(mispricing, 1),
        "reverse": round(raw_reverse, 1),
        "total_score": max(0.0, min(10.0, composite)),
        "data_confidence": confidence,
        "data_status": {
            "available": available,
            "total": len(expected),
            "errors": errors,
            "as_of": datetime.now().astimezone().isoformat(timespec="seconds"),
        },
        "quality_gate": {"status": status, "passed": status == "pass", "reasons": reasons},
    })
    return row


def _batch_snapshot(client, codes):
    parts = []
    for i in range(0, len(codes), 100):
        chunk = codes[i:i + 100]
        snap, _ = client.market_snapshot(chunk)
        if snap is not None and not snap.empty:
            parts.append(snap)
    if not parts:
        return None
    return pd.concat(parts, ignore_index=True)


def _hstech_drop(client, hstech_code: str) -> Optional[float]:
    """抓取恒科指数涨跌幅（用于联动策略）；失败返回 None。"""
    try:
        snap, err = client.market_snapshot([hstech_code])
        if err or snap is None or snap.empty:
            return None
        cols = {c.lower(): c for c in snap.columns}
        chg_c = cols.get("change_rate")
        prev_c = cols.get("prev_close_price")
        price_c = cols.get("last_price")
        chg = _num(snap.iloc[0][chg_c]) if chg_c else None
        if chg is None and price_c and prev_c:
            price = _num(snap.iloc[0][price_c])
            prev = _num(snap.iloc[0][prev_c])
            if price and prev:
                chg = round((price - prev) / prev * 100, 2)
        return chg
    except Exception:  # noqa: BLE001
        return None


def _cash_state(cash_ratio: Optional[float], thresholds: dict, cash_full_stop: bool):
    """返回 (仓位状态文字, 推送门槛, 是否停推)。

    cash_ratio 为占仓比例的**小数**（如 0.21 表示现金占 21%），
    故阈值用 0.30 / 0.15 比较（不要与百分比整数混淆）。
    """
    if cash_ratio is None:
        return "中仓(未知)", thresholds["mid"], False
    if cash_ratio >= 0.30:
        return "轻仓", thresholds["light"], False
    if cash_ratio >= 0.15:
        return "中仓", thresholds["mid"], False
    # <15% 视为满仓
    return "满仓", thresholds["mid"], cash_full_stop


def screen(client, codes: Optional[list] = None, top_n: int = 20,
           hstech_code: str = "HK.800700", thresholds: Optional[dict] = None,
           cash_full_stop: bool = True, cash_ratio: Optional[float] = None,
           with_reverse: bool = True, cfg: Optional[dict] = None):
    """
    买入机会扫描。返回 (dict, error)。
    dict: {results[], cash_ratio, cash_state, push_threshold, stop_push, hstech_drop, count}
    cfg: 策略参数（来自 strategy_config）；为空时自动加载 strategies.yaml。
    """
    cfg = cfg or strategy_config.load_config()
    thresholds = thresholds or cfg.get("push", DEFAULT_THRESHOLDS)
    watch = codes if codes else LEADERS
    snap = _batch_snapshot(client, watch)
    if snap is None:
        return None, "快照获取失败（FutuOpenD 未连接或代码无效）"

    # 恒科联动信号：指数急跌时龙头低吸（阈值取自配置）
    hstech = _hstech_drop(client, hstech_code)
    hstech_th = cfg.get("strategies", {}).get("hstech_link", {}).get("hstech_drop", -2.0)
    hstech_crash = (hstech is not None and hstech <= hstech_th)

    # 趋势特征（sma/rsi2）：仅在趋势过滤器或 RSI2 开启时按需拉取，带缓存
    need_trend = (cfg.get("trend_filter", {}).get("enabled", False)
                  or cfg.get("strategies", {}).get("rsi2_connor", {}).get("enabled", False))
    trend_map: dict = {}
    if need_trend:
        for _c in watch:
            trend_map[_c] = _latest_trend(client, _c, 250)

    # 现金比例（仓位感知）
    if cash_ratio is None:
        try:
            cash_ratio, _, _ = client.cash_ratio()
        except Exception:  # noqa: BLE001
            cash_ratio = None
    cash_state, push_th, stop_push = _cash_state(cash_ratio, thresholds, cash_full_stop)

    code_c = _col(snap, "code")
    name_c = _col(snap, "name") or _col(snap, "stock_name")
    price_c = _col(snap, "last_price")
    chg_c = _col(snap, "change_rate")
    prev_close_c = _col(snap, "prev_close_price")
    turn_c = _col(snap, "turnover_rate")
    amp_c = _col(snap, "amplitude")
    pe_c = _col(snap, "pe_ratio")
    hi_c = _col(snap, "highest52weeks_price") or _col(snap, "52_week_high")
    lo_c = _col(snap, "lowest52weeks_price") or _col(snap, "52_week_low")
    vol_c = _col(snap, "volume")
    susp_c = _col(snap, "suspension")

    leaders_set = set(LEADERS)
    results = []
    for _, row in snap.iterrows():
        code = str(row[code_c]) if code_c else ""
        # 自动剔除停牌 / 无报价标的（如已停牌老千股 last_price=0）
        if susp_c and row[susp_c] is True:
            continue
        price = _num(row[price_c]) if price_c else None
        price = round(price, 3) if price is not None else None
        # 无报价（last_price=0 或无行情）直接剔除，不参与扫描
        if not price or price <= 0:
            continue
        chg = _num(row[chg_c]) if chg_c else None
        prev_close = _num(row[prev_close_c]) if prev_close_c else None
        turn = _num(row[turn_c]) if turn_c else 0.0
        amp = _num(row[amp_c]) if amp_c else None
        pe = _num(row[pe_c]) if pe_c else None
        pe = round(pe, 2) if pe is not None else None
        hi = _num(row[hi_c]) if hi_c else None
        lo = _num(row[lo_c]) if lo_c else None
        if chg is None and price is not None and prev_close:
            chg = round((price - prev_close) / prev_close * 100, 2)
        if chg is None:
            chg = 0.0

        pos_pct = None
        if price and hi and lo and hi > lo:
            pos_pct = (price - lo) / (hi - lo) * 100
        # 统一走 evaluate_signals 纯函数（实盘与回测共用）
        tr = trend_map.get(code, {})
        feats = {
            "code": code,
            "name": str(row[name_c]) if name_c else code,
            "price": price,
            "change_rate": chg,
            "prev_close_price": prev_close,
            "turnover_rate": turn,
            "amplitude": amp,
            "pe": pe,
            "hi52": hi,
            "lo52": lo,
            "pos_pct": pos_pct,
            "is_leader": code in leaders_set,
            "hstech_crash": hstech_crash,
            "sma50": tr.get("sma50"),
            "sma200": tr.get("sma200"),
            "rsi2": tr.get("rsi2"),
        }
        ev = evaluate_signals(feats, cfg)
        signals = ev["signals"]
        score = ev["score"]

        results.append({
            "code": code,
            "name": str(row[name_c]) if name_c else code,
            "price": round(price, 3) if price is not None else None,
            "change_rate": chg,
            "turnover_rate": turn,
            "pe": pe,
            "week52_position_pct": round(pos_pct, 1) if pos_pct is not None else None,
            "score": score,
            "signals": signals,
            "signal_details": ev.get("signal_details", []),
            "reason_inputs": ev.get("reason_inputs", {}),
            "reason": _build_reason(ev.get("reason_inputs", {}), signals),
        })

    results = _clean_nums(results)
    results.sort(key=lambda x: (x["score"] is None, x["score"] or 0), reverse=True)

    # 反向信号加权（错杀增强）：仅对基础分靠前的候选拉取南向/回购/新闻三块，
    # 控制 API 调用量；其余候选 reverse=0，不影响排序。
    # with_reverse=False 时（如定时推送场景）跳过反向调用，控制 API 频率。
    if with_reverse:
        # Reverse evidence affects ranking, so every candidate must be evaluated;
        # pre-filtering by technical score would make strong mispricing candidates invisible.
        REVERSE_CAP = len(results)
        top_codes = [r["code"] for r in results]
        rev_map = {}
        if top_codes:
            try:
                rev_map = reverse_signals.reverse_score_batch(
                    client, top_codes, days=60, num=10, cfg=cfg
                )
            except Exception:  # noqa: BLE001
                rev_map = {}
        for r in results[:REVERSE_CAP]:
            rev, _ = rev_map.get(r["code"], (None, None))
            if rev:
                r["reverse_signals"] = rev["signals"]
                r["reverse_details"] = rev["details"]
            else:
                r["reverse_signals"] = []
                r["reverse_details"] = {}
            _apply_score_layers(r, rev)
        for r in results[REVERSE_CAP:]:
            r["reverse_signals"] = []
            r["reverse_details"] = {}
            _apply_score_layers(r, None)
    else:
        for r in results:
            r["reverse_signals"] = []
            r["reverse_details"] = {}
            _apply_score_layers(r, None)

    # 按「基础分 + 反向加分」总分重排，使错杀反向信号影响排序
    results.sort(key=lambda x: (x["total_score"] is None, x["total_score"] or 0),
                 reverse=True)

    top = results[:top_n]
    # 推送门槛纳入总分：基础分达标 或 反向信号强化后总分达标，均触发买入机会通知。
    # 这样「技术面基础分略低但南向/回购/新闻强烈反向看好」的错杀候选也能进入推送。
    for r in top:
        technical_trigger = (r["technical_score"] or 0) >= push_th
        research_trigger = (
            (r["total_score"] or 0) >= push_th
            and r["data_confidence"] >= 50
            and r["quality_gate"]["status"] != "fail"
        )
        r["push"] = (not stop_push) and (technical_trigger or research_trigger)
        r["trigger_source"] = (
            "technical_backtested" if technical_trigger
            else "multi_source_unvalidated" if research_trigger
            else None
        )
    return _clean_nan({
        "results": top,
        "cash_ratio": cash_ratio,
        "cash_state": cash_state,
        "push_threshold": push_th,
        "stop_push": stop_push,
        "hstech_drop": hstech,
        "count": len(top),
    }), None


def _build_reason(ri: dict, signals: list) -> str:
    """根据特征与命中信号，生成一句话 contrarian 逻辑（用于推送「原因」）。

    ri: evaluate_signals 返回的 reason_inputs（drop_pct/rsi2/uptrend/pe/pos_pct/chg/turn）。
    """
    if not signals:
        return ""
    parts = []
    uptrend = ri.get("uptrend")
    for sig in signals:
        if sig == "深度超跌反弹":
            d = ri.get("drop_pct")
            parts.append(f"距52周高点回撤{d:.0f}%（超跌区）" if d is not None else "进入52周超跌区")
        elif sig == "RSI2 逆向低吸":
            r = ri.get("rsi2")
            parts.append(f"RSI2={r:.0f} 极度超卖（<5）" if r is not None else "RSI2 极度超卖")
        elif sig == "恒科急跌联动低吸":
            parts.append("恒科指数急跌触发，个股跟跌错杀")
        elif sig == "放量突破":
            c = ri.get("chg")
            parts.append(f"放量上涨{c:+.1f}%、换手放大，突破态势" if c is not None else "放量突破")
        elif sig == "异常放量急跌(逆向)":
            c = ri.get("chg")
            parts.append(f"日内大跌{c:+.1f}%且放量，逆向博反弹" if c is not None else "放量大跌逆向博反弹")
        elif sig == "低PE低位":
            pe = ri.get("pe")
            p = ri.get("pos_pct")
            parts.append(f"PE={pe} 且处52周低位{p:.0f}%"
                         if (pe is not None and p is not None) else "低PE且接近52周低位")
        elif sig == "龙头观察池":
            parts.append("龙头标签（非触发信号）")
    base = "；".join(p for p in parts if p)
    if uptrend:
        return f"上升趋势中：{base} → 趋势内回调/低吸，非接飞刀"
    return f"{base}（注：处下跌趋势，接飞刀风险高，谨慎）"


# 回测背书静态兜底（来源：/backtest/sweep 验证 2026-08-03，forward=10/stop=0.04/rsi2=5）
_BACKTEST_EVIDENCE_FALLBACK = {
    "深度超跌反弹": (59.0, 0.95),
    "放量突破": (51.5, 1.28),
    "低PE低位": (80.0, 5.61),
    "恒科急跌联动低吸": (54.3, 0.82),
    "异常放量急跌(逆向)": (40.4, 0.67),
    "RSI2 逆向低吸": (61.1, 1.19),
}


def _load_backtest_stats() -> Optional[dict]:
    """取回测缓存的 per_strategy 统计；无缓存返回 None（回退静态表）。局部 import 避免循环依赖。"""
    try:
        from .. import bt_backtest
        return bt_backtest.get_cached_backtest_stats()
    except Exception:  # noqa: BLE001
        return None


def _backtest_evidence(signals: list) -> str:
    """为命中信号拼回测背书文本：『策略 历史胜率X%/盈利因子Y』。"""
    stats = _load_backtest_stats()
    out = []
    for sig in signals:
        if sig == "龙头观察池":
            continue
        st = (stats or {}).get(sig)
        if st and st.get("win_rate") is not None:
            note = "（样本不足）" if (st.get("n") or 0) < 20 else ""
            out.append(f"{sig} 历史胜率{st['win_rate']}%/盈利因子{st['profit_factor']}{note}")
        else:
            fb = _BACKTEST_EVIDENCE_FALLBACK.get(sig)
            if fb:
                out.append(f"{sig} 回测胜率约{fb[0]}%/盈利因子{fb[1]}(参考)")
    return "；".join(out) if out else "—"


def _signal_markdown(r: dict, result: dict) -> str:
    """构建单只 6 策略买入信号的企业微信 markdown（4096 字节保护）。

    含：分数来源拆解 + 一句话原因 + 回测背书 + 止损（与回测一致的 -4%）。
    """
    cash = result.get("cash_state", "")
    cash_ratio = result.get("cash_ratio")
    cr = f"{cash_ratio * 100:.0f}%" if isinstance(cash_ratio, (int, float)) else "—"
    sd = r.get("signal_details") or []
    src = " + ".join(f"{d['name']}{d['weight']:+.1f}" for d in sd) if sd else "—"
    reason = r.get("reason") or "—"
    ev = _backtest_evidence(r.get("signals", []))
    lines = [
        "🔵 **6大策略买入信号**",
        f"> {r.get('name', '')}({r.get('code', '')})",
        f"> 现价: {r.get('price')}  涨跌: {r.get('change_rate')}%",
        f"> 基础分: {r.get('score') or 0}  反向加分: +{r.get('reverse') or 0}  **总分: {r.get('total_score') or 0}**",
        f"> 分数来源: {src}",
        f"> 原因: {reason}",
        f"> 回测背书: {ev}",
        f"> 仓位: {cash}(现金{cr})",
        "💡 首批建仓≤1/3，止损 -4%（与回测一致）",
    ]
    text = "\n".join(lines)
    if len(text.encode("utf-8")) > 4000:
        text = text[:3900] + "\n…(截断)"
    return text


def run_scheduled_scan(client, codes: Optional[list] = None, webhook: str = "",
                       hstech_code: str = "HK.800700", cash_full_stop: bool = True,
                       cooldown_sec: int = 14400, max_push: int = 5, force: bool = False):
    """交易时段定时扫描：跑全 6 策略，仓位感知 + 指纹去重推送。

    - codes 为空时用 LEADERS 龙头池
    - stop_push(满仓) 时静默返回，不推送（满仓预警由 monitor 负责，避免重复）
    - force=True 时忽略冷却立即推送（供手动按钮调用）
    返回 dict: {scan_type, scanned, pushed, signals, stop_push?, error?, note?}
    """
    if not webhook:
        return {"scan_type": "six_strategy", "scanned": 0, "pushed": 0,
                "signals": [], "note": "webhook 未配置，跳过推送"}
    result, err = screen(client, codes=codes, top_n=50, hstech_code=hstech_code,
                         cash_full_stop=cash_full_stop, with_reverse=True)
    if err or not result:
        return {"scan_type": "six_strategy", "scanned": 0, "pushed": 0,
                "signals": [], "error": err or "no result"}
    if result.get("stop_push"):
        return {"scan_type": "six_strategy", "scanned": 0, "pushed": 0,
                "signals": [], "stop_push": True}
    eligible = [r for r in result["results"] if r.get("push")]
    pushed = 0
    out = []
    for r in eligible[:max_push]:
        md = _signal_markdown(r, result)
        fp = f"six_strategy:{r['code']}"
        ok = notify.push_wecom(md, webhook) if force else notify.push_if_new(
            fp, md, webhook, min_interval=cooldown_sec)
        if ok:
            pushed += 1
            out.append({"code": r["code"], "name": r["name"], "score": r["total_score"]})
    return {"scan_type": "six_strategy", "scanned": len(eligible),
            "pushed": pushed, "signals": out}

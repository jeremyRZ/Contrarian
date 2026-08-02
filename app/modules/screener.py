"""
选股筛选 / 买入机会扫描模块
- 基于富途观察池快照
- 6 大策略信号 + 10 分制综合评分（复刻 hk-stock-monitor scanner 思路）
- 仓位感知：根据现金比例动态调整推送门槛（轻仓6分 / 中仓7分 / 满仓停推）
"""
from __future__ import annotations

import math
from typing import Optional

import pandas as pd

from . import reverse_signals
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
           with_reverse: bool = True):
    """
    买入机会扫描。返回 (dict, error)。
    dict: {results[], cash_ratio, cash_state, push_threshold, stop_push, hstech_drop, count}
    """
    thresholds = thresholds or DEFAULT_THRESHOLDS
    watch = codes if codes else LEADERS
    snap = _batch_snapshot(client, watch)
    if snap is None:
        return None, "快照获取失败（FutuOpenD 未连接或代码无效）"

    # 恒科联动信号：指数急跌时龙头低吸
    hstech = _hstech_drop(client, hstech_code)
    hstech_crash = (hstech is not None and hstech <= -2.0)

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

        signals = []
        score = 0.0

        # 1. 深度超跌反弹：距52周高点回撤 >25% 且盈利
        drop = None
        if price and hi and hi > lo and hi > 0:
            drop = (hi - price) / hi * 100
            if drop > 25 and (pe is None or pe > 0):
                signals.append("深度超跌反弹")
                score += 3
        # 2. 放量突破：上涨且换手/量能活跃
        if (chg or 0) > 2 and (turn or 0) > 1.5:
            signals.append("放量突破")
            score += 3
        # 3. 低估值高股息（近似）：PE<10 且接近52周低位
        pos_pct = None
        if price and hi and lo and hi > lo:
            pos_pct = (price - lo) / (hi - lo) * 100
        if pe and pe < 10 and (pos_pct is not None and pos_pct < 30):
            signals.append("低估值高股息")
            score += 3
        # 4. 恒科急跌联动低吸：指数急跌时龙头跟跌即低吸机会
        if hstech_crash and code in leaders_set and (chg or 0) < 0:
            signals.append("恒科急跌联动低吸")
            score += 2
        # 5. 恐慌急跌（逆向）：日内大跌且放量
        if (chg or 0) < -5 and (turn or 0) > 2:
            signals.append("异常放量急跌(逆向)")
            score += 2
        # 6. 龙头观察池
        if code in leaders_set:
            signals.append("龙头观察池")
            score += 1

        score = min(10.0, round(score, 1))
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
        })

    results = _clean_nums(results)
    results.sort(key=lambda x: (x["score"] is None, x["score"] or 0), reverse=True)

    # 反向信号加权（错杀增强）：仅对基础分靠前的候选拉取南向/回购/新闻三块，
    # 控制 API 调用量；其余候选 reverse=0，不影响排序。
    # with_reverse=False 时（如定时推送场景）跳过反向调用，控制 API 频率。
    if with_reverse:
        REVERSE_CAP = 15
        top_codes = [r["code"] for r in results[:REVERSE_CAP]]
        rev_map = {}
        if top_codes:
            try:
                rev_map = reverse_signals.reverse_score_batch(client, top_codes, days=60, num=10)
            except Exception:  # noqa: BLE001
                rev_map = {}
        for r in results[:REVERSE_CAP]:
            rev, _ = rev_map.get(r["code"], (None, None))
            if rev:
                r["reverse"] = rev["score"]
                r["reverse_signals"] = rev["signals"]
                r["reverse_details"] = rev["details"]
            else:
                r["reverse"] = 0.0
                r["reverse_signals"] = []
                r["reverse_details"] = {}
            r["total_score"] = round(r["score"] + r["reverse"], 1)
        for r in results[REVERSE_CAP:]:
            r["reverse"] = 0.0
            r["reverse_signals"] = []
            r["reverse_details"] = {}
            r["total_score"] = r["score"]
    else:
        for r in results:
            r["reverse"] = 0.0
            r["reverse_signals"] = []
            r["reverse_details"] = {}
            r["total_score"] = r["score"]

    # 按「基础分 + 反向加分」总分重排，使错杀反向信号影响排序
    results.sort(key=lambda x: (x["total_score"] is None, x["total_score"] or 0),
                 reverse=True)

    top = results[:top_n]
    # 推送门槛纳入总分：基础分达标 或 反向信号强化后总分达标，均触发买入机会通知。
    # 这样「技术面基础分略低但南向/回购/新闻强烈反向看好」的错杀候选也能进入推送。
    for r in top:
        r["push"] = (not stop_push) and (
            (r["score"] or 0) >= push_th or (r["total_score"] or 0) >= push_th)
    return _clean_nan({
        "results": top,
        "cash_ratio": cash_ratio,
        "cash_state": cash_state,
        "push_threshold": push_th,
        "stop_push": stop_push,
        "hstech_drop": hstech,
        "count": len(top),
    }), None


def _signal_markdown(r: dict, result: dict) -> str:
    """构建单只 6 策略买入信号的企业微信 markdown（4096 字节保护）。"""
    cash = result.get("cash_state", "")
    cash_ratio = result.get("cash_ratio")
    cr = f"{cash_ratio:.0f}%" if isinstance(cash_ratio, (int, float)) else "—"
    sigs = "、".join(r.get("signals", [])) or "—"
    lines = [
        "🔵 **6大策略买入信号**",
        f"> {r.get('name', '')}({r.get('code', '')})",
        f"> 现价: {r.get('price')}  涨跌: {r.get('change_rate')}%",
        f"> 基础分: {r.get('score') or 0}  反向加分: +{r.get('reverse') or 0}  **总分: {r.get('total_score') or 0}**",
        f"> 命中: {sigs}",
        f"> 仓位: {cash}(现金{cr})",
        "💡 首批建仓≤目标仓位1/3，正股止损参考 -8%",
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
                         cash_full_stop=cash_full_stop, with_reverse=False)
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

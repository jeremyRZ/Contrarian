"""
持仓监控风控模块
- 读取富途持仓，按品种分类做止损/止盈信号检测
- 含手数感知与阶梯止盈（复刻 hk-stock-monitor monitor 纪律体系）
"""
from __future__ import annotations

import pandas as pd

# 止损纪律（百分比）
STOP_RULES = {
    "正股(趋势)": 8.0,
    "正股(抄底)": 12.0,
    "杠杆ETF": 5.0,
    "窝轮": 30.0,
    "ETF": 8.0,
}
# 阶梯止盈（百分比阈值 -> 动作）
TP_LADDER = [
    (10, "减仓1/4，止损上移至成本价"),
    (25, "减仓1/4，止损上移至+10%"),
    (40, "减仓1/4，启用移动止盈(回撤-12%清仓)"),
]

# 富途港股期权代码使用英文标的缩写，持仓接口本身不返回正股代码。
# 在数据层统一维护映射，分析页与风险页无需各自猜测。
DERIVATIVE_UNDERLYINGS = {
    "MIU": {"code": "HK.01810", "name": "小米集团-W"},
    "SMC": {"code": "HK.00981", "name": "中芯国际"},
}


def derivative_underlying(code: str, name: str = "") -> dict | None:
    """返回受支持的港股期权或窝轮对应正股。"""
    symbol = str(code or "").upper().removeprefix("HK.")
    for prefix, stock in DERIVATIVE_UNDERLYINGS.items():
        if symbol.startswith(prefix):
            return dict(stock)
    for keyword, prefix in (("小米", "MIU"), ("中芯", "SMC")):
        if keyword in str(name or ""):
            return dict(DERIVATIVE_UNDERLYINGS[prefix])
    return None


def _classify(name: str, code: str) -> str:
    n = (name or "")
    if "杠杆" in n or "2X" in n or "3X" in n or "XL" in n.upper() or "两倍" in n or "三倍" in n:
        return "杠杆ETF"
    if any(k in n for k in ("购", "沽", "牛", "熊", "证", "Call", "Put", "CBBC")):
        return "窝轮"
    return "正股(趋势)"


def _lots(qty, lot_size):
    if not lot_size or lot_size <= 0:
        return None
    return int(qty // lot_size)


def _num(v):
    try:
        if v is None or (isinstance(v, str) and v.strip().upper() in ("N/A", "NA", "")):
            return None
        f = float(v)
        return None if f != f else f
    except (ValueError, TypeError):
        return None


def _tech_kline(client, code: str) -> dict:
    """抓取日K，计算 RSI14 / MA20 / 量能比 / 上影线比例。失败返回空字典。"""
    try:
        k, err = client.history_kline(code, max_count=25)
        if err or k is None or k.empty:
            return {}
        cols = {c.lower(): c for c in k.columns}
        closes = [_num(x) for x in k["close"].tolist()]
        closes = [c for c in closes if c is not None]
        # RSI14（Wilder）
        if len(closes) > 14:
            deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
            g = [max(d, 0.0) for d in deltas]
            l = [max(-d, 0.0) for d in deltas]
            ag = sum(g[:14]) / 14
            al = sum(l[:14]) / 14
            for i in range(14, len(deltas)):
                ag = (ag * 13 + g[i]) / 14
                al = (al * 13 + l[i]) / 14
            rsi = 100.0 if al == 0 else round(100 - 100 / (1 + ag / al), 1)
        else:
            rsi = None
        ma20 = round(sum(closes[-20:]) / 20, 3) if len(closes) >= 20 else None
        # 量能比（最后一根 / 前5根均值）
        vol_ratio = None
        if "volume" in cols:
            vol = [_num(x) for x in k["volume"].tolist()]
            vol = [v for v in vol if v]
            if len(vol) >= 6 and sum(vol[-6:-1]) > 0:
                vol_ratio = round(vol[-1] / (sum(vol[-6:-1]) / 5), 2)
        # 上影线比例（最后一根）
        upper_wick = None
        if all(c in cols for c in ("high", "low", "open", "close")):
            high = _num(k.iloc[-1][cols["high"]])
            low = _num(k.iloc[-1][cols["low"]])
            op = _num(k.iloc[-1][cols["open"]])
            cl = _num(k.iloc[-1][cols["close"]])
            if high and low and high > low:
                body_top = max(op, cl)
                upper_wick = round((high - body_top) / (high - low), 2)
        return {"rsi14": rsi, "ma20": ma20, "vol_ratio": vol_ratio,
                "upper_wick_ratio": upper_wick}
    except Exception:  # noqa: BLE001
        return {}


def monitor_positions(client, technical: bool = True):
    """
    持仓风控扫描。返回 (result_dict, error)
    """
    pos, err = client.positions()
    if err:
        return None, err
    if pos is None or pos.empty:
        return {"positions": [], "summary": {"count": 0, "total_market_val": 0,
                                              "total_pl_val": 0}}, None

    # 取 lot_size 映射（失败则忽略）
    lot_map = {}
    try:
        codes = [c for c in pos["code"].tolist()]
        bi, _ = client.stock_basicinfo()
        if bi is not None and not bi.empty and "code" in bi.columns:
            for _, r in bi.iterrows():
                lot_map[r["code"]] = r.get("lot_size", 0)
    except Exception:  # noqa: BLE001
        lot_map = {}

    cols = {c.lower(): c for c in pos.columns}
    c_code = cols.get("code")
    c_name = cols.get("stock_name") or cols.get("name")
    c_qty = cols.get("qty")
    c_cost = cols.get("cost_price")
    c_mv = cols.get("market_val")
    c_plr = cols.get("pl_ratio")
    c_plv = cols.get("pl_val")

    positions = []
    total_mv = 0.0
    total_pl = 0.0
    for _, row in pos.iterrows():
        code = str(row[c_code])
        name = str(row[c_name]) if c_name else code
        qty = float(row[c_qty]) if c_qty else 0.0
        cost = float(row[c_cost]) if c_cost else 0.0
        mv = float(row[c_mv]) if c_mv else 0.0
        plv = float(row[c_plv]) if c_plv else 0.0
        plr = float(row[c_plr]) if c_plr else 0.0  # futu: 百分比

        cur_price = (mv / qty) if qty else 0.0
        # 用市价重算更准
        if cost and cur_price:
            plr = (cur_price - cost) / cost * 100

        ptype = _classify(name, code)

        # 衍生品（窝轮/杠杆ETF）的 qty/mv/cost 语义完全不同，
        # cur_price=mv/qty 算出的价格是垃圾数据，直接跳过不做风控计算。
        if ptype in ("窝轮", "杠杆ETF"):
            positions.append({
                "code": code,
                "name": name,
                "qty": qty,
                "lots": None,
                "cur_price": None,
                "market_val": round(mv, 2) if mv else 0,
                "pl_ratio": None,
                "pl_val": round(plv, 2) if plv else 0,
                "type": ptype,
                "stop_pct": STOP_RULES.get(ptype, 8.0),
                "stop_loss_price": None,
                "tp_hit": [],
                "scale": "衍生品(不适用)",
                "signals": [],
                "advice": f"{ptype}，不适用正股止损/止盈逻辑",
                "rsi14": None,
                "ma20": None,
                "vol_ratio": None,
            })
            total_mv += mv
            total_pl += plv
            continue

        stop_pct = STOP_RULES.get(ptype, 8.0)
        stop_price = round(cost * (1 - stop_pct / 100), 3)
        lots = _lots(qty, lot_map.get(code, 0))

        signals = []
        if plr <= -stop_pct:
            signals.append("⚠️ 触及止损线")
        tp_hit = []
        for th, act in TP_LADDER:
            if plr >= th:
                tp_hit.append({"pct": th, "action": act})
        # 只取最高触达的阶梯作为一条信号，避免窝轮/杠杆ETF 一次触发多层刷屏
        if tp_hit:
            max_th = max(t["pct"] for t in tp_hit)
            signals.append(f"到达+{max_th}%减仓线")

        # 技术面止盈（放量滞涨 / 长上影线 / RSI超买）
        tech = {}
        if technical:
            tech = _tech_kline(client, code)
            rsi = tech.get("rsi14")
            vol_ratio = tech.get("vol_ratio")
            wick = tech.get("upper_wick_ratio")
            if rsi is not None and rsi >= 70:
                signals.append("🔔 RSI超买(技术止盈)")
            if vol_ratio and vol_ratio >= 2 and abs(plr) < 1.5:
                signals.append("🔔 放量滞涨(技术止盈)")
            if wick is not None and wick >= 0.6:
                signals.append("🔔 长上影线(技术止盈)")

        # 手数感知建议
        if lots is None:
            scale = "未知手数"
        elif lots <= 1:
            scale = "1手(不可分批)"
        elif lots <= 3:
            scale = f"{lots}手(粗略分批)"
        else:
            scale = f"{lots}手(正常阶梯)"

        advice = _advice(ptype, plr, stop_pct, lots, tp_hit)

        total_mv += mv
        total_pl += plv
        positions.append({
            "code": code,
            "name": name,
            "qty": qty,
            "lots": lots,
            "lot_size": lot_map.get(code, 0),
            "cost_price": round(cost, 3) if cost else None,
            "cur_price": round(cur_price, 3),
            "market_val": round(mv, 2),
            "pl_ratio": round(plr, 2),
            "pl_val": round(plv, 2),
            "type": ptype,
            "stop_pct": stop_pct,
            "stop_loss_price": stop_price,
            "tp_hit": tp_hit,
            "scale": scale,
            "signals": signals,
            "advice": advice,
            "rsi14": tech.get("rsi14"),
            "ma20": tech.get("ma20"),
            "vol_ratio": tech.get("vol_ratio"),
        })

    positions.sort(key=lambda x: x["pl_ratio"] if x.get("pl_ratio") is not None else -9999)

    # 汇总需推送预警的项：止损触发(danger) + 止盈阶梯命中(info)
    alerts = []
    for p in positions:
        sigs = p.get("signals", [])
        if any("触及止损线" in s for s in sigs):
            alerts.append({
                "code": p["code"], "name": p["name"], "level": "danger",
                "msg": f"已跌破{p['type']}止损线(-{p['stop_pct']}%)，现价{p['cur_price']}≤止损价{p['stop_loss_price']}",
            })
        for s in sigs:
            if "减仓线" in s:
                alerts.append({
                    "code": p["code"], "name": p["name"], "level": "info",
                    "msg": s,
                })
        for s in sigs:
            if "技术止盈" in s and p.get("pl_ratio", 0) > 0:
                alerts.append({
                    "code": p["code"], "name": p["name"], "level": "info",
                    "msg": s,
                })

    return {
        "positions": positions,
        "alerts": alerts,
        "summary": {
            "count": len(positions),
            "total_market_val": round(total_mv, 2),
            "total_pl_val": round(total_pl, 2),
        },
    }, None


def _advice(ptype, plr, stop_pct, lots, tp_hit):
    if plr <= -stop_pct:
        return f"已跌破{ptype}止损线(-{stop_pct}%)，建议止损离场"
    if tp_hit:
        last = tp_hit[-1]
        if lots is not None and lots <= 1:
            return (f"盈利{last['pct']}%，但仅1手无法分批；人工选择整手止盈，"
                    "或继续持有并把止损上移至成本价")
        return f"盈利{last['pct']}%，建议：{last['action']}"
    if plr > 0:
        return f"持仓盈利 +{round(plr,2)}%，未到减仓线，持有观察"
    return f"浮亏 {round(plr,2)}%，未触止损(-{stop_pct}%)，持有观察"

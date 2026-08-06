"""盘中「恒科急跌联动」低吸扫描。

场景：恒生科技指数（HK.800700）盘中急跌时，持仓 / 本地观察池 / 龙头池里
「跟跌」的个股往往出现错杀低吸窗口。本模块：

  1) 抓恒科指数实时状态（涨跌% + 日内回撤），判定是否「急跌」（阈值默认 -2%）
  2) 汇总候选池（持仓正股 + 本地观察池 + 龙头池），过滤窝轮/杠杆ETF/停牌/无报价
  3) 只对「跟跌」个股做 8 档反向信号打分，叠加「深度超跌 + 跟跌幅度」算出
     「低吸吸引力评分」，并给出操作建议
  4) 生成可直接推送企业微信的 markdown（受 4096 字节保护）

所有外部数据调用失败都优雅降级（该项计缺失），不抛异常。
"""
from __future__ import annotations

import logging
import math
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import pandas as pd

from . import filters, monitor, reverse_signals
from .. import notify

logger = logging.getLogger("hk-intraday")

# 恒生科技指数（正确代码；与 screener 历史使用的 HK.800000 恒生指数区分）
HSTECH_DEFAULT = "HK.800700"
HSTECH_NAME = "恒生科技指数"
THRESHOLD_DEFAULT = -2.0          # 急跌阈值（涨跌%）
REVERSE_CAP = 20                 # 单轮反向信号打分上限（控制 API 压力）
TOP_PUSH = 15                    # 推送 markdown 最多展示候选数
MAX_BYTES = 3800                 # 微信 markdown 单条上限（留余量）
# 恒科联动策略回测背书（来源 /backtest/sweep 2026-08-03，forward=10/stop=0.04/rsi2=5）
HSTECH_EVIDENCE = "📊 恒科联动低吸历史回测：胜率54%/盈利因子0.82（持有10日/止损4%）"


def _num(v):
    """安全转 float；'N/A'/NaN/None/空 → None。"""
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


def _batch_snapshot(client, codes: List[str]):
    """分批抓取行情快照，拼接为单个 DataFrame；失败返回 None。"""
    if not codes:
        return None
    parts = []
    for i in range(0, len(codes), 100):
        chunk = codes[i:i + 100]
        snap, _ = client.market_snapshot(chunk)
        if snap is not None and not snap.empty:
            parts.append(snap)
    if not parts:
        return None
    return pd.concat(parts, ignore_index=True)


def _index_status(client, hstech_code: str) -> dict:
    """抓取恒科指数状态。返回 {code,name,change_rate,last_price,prev_close,
    high,low,intraday_drawdown,crash,error}。失败优雅降级。"""
    out = {"code": hstech_code, "name": HSTECH_NAME, "change_rate": None,
           "last_price": None, "prev_close": None, "high": None, "low": None,
           "intraday_drawdown": None, "crash": False, "error": None}
    snap, err = client.market_snapshot([hstech_code])
    if err or snap is None or snap.empty:
        out["error"] = err or "快照为空"
        return out
    cols = {c.lower(): c for c in snap.columns}
    row = snap.iloc[0]
    chg = _num(row[cols["change_rate"]]) if "change_rate" in cols else None
    price = _num(row[cols["last_price"]]) if "last_price" in cols else None
    prev = _num(row[cols["prev_close_price"]]) if "prev_close_price" in cols else None
    high = _num(row[cols["high"]]) if "high" in cols else None
    low = _num(row[cols["low"]]) if "low" in cols else None
    if chg is None and price and prev:
        chg = round((price - prev) / prev * 100, 2)
    dd = None
    if price and high and high > 0:
        dd = round((price - high) / high * 100, 2)
    out.update({"change_rate": chg, "last_price": price, "prev_close": prev,
                "high": high, "low": low, "intraday_drawdown": dd})
    return out


def _tradable_from_row(row, cols) -> bool:
    """从快照行直接判断可交易（停牌 / 无报价 / 无估值 → False），避免重复拉快照。"""
    susp_c = cols.get("suspension")
    if susp_c and row[susp_c] is True:
        return False
    lp = _num(row[cols["last_price"]]) if "last_price" in cols else None
    if lp is None or lp <= 0:
        return False
    pe = _num(row[cols["pe_ratio"]]) if "pe_ratio" in cols else None
    pb = _num(row[cols["pb_ratio"]]) if "pb_ratio" in cols else None
    pe = pe or 0.0
    pb = pb or 0.0
    if pe <= 0 and pb <= 0:
        return False
    return True


def scan_linkage(client, hstech_code: str = HSTECH_DEFAULT,
                 threshold: float = THRESHOLD_DEFAULT,
                 codes: Optional[List[str]] = None,
                 code_meta: Optional[Dict[str, dict]] = None) -> dict:
    """执行一次「恒科急跌联动」低吸扫描。

    参数：
      client: FutuClient
      hstech_code: 指数代码（默认 HK.800700 恒生科技指数）
      threshold: 急跌阈值（涨跌%，默认 -2.0）
      codes: 候选代码列表（持仓 + 观察池 + 龙头池，已在调用方汇总去重）
      code_meta: {code: {"name":..,"source":..}} 候选来源标注（可选）

    返回 dict：{ok, index, threshold, crash, candidates[], markdown, errors}
    """
    code_meta = code_meta or {}
    codes = codes or []

    # 1) 指数状态
    idx = _index_status(client, hstech_code)
    crash = (idx.get("change_rate") is not None
             and idx["change_rate"] <= threshold)

    # 2) 候选快照
    snap = _batch_snapshot(client, codes)
    errors: List[str] = []
    if idx.get("error"):
        errors.append(f"指数数据：{idx['error']}")
    if snap is None:
        errors.append("候选快照获取失败（FutuOpenD 未连接或代码无效）")

    candidates: List[dict] = []
    if snap is not None and not snap.empty:
        cols = {c.lower(): c for c in snap.columns}
        code_c = cols.get("code")
        name_c = cols.get("name") or cols.get("stock_name")
        price_c = cols.get("last_price")
        chg_c = cols.get("change_rate")
        prev_c = cols.get("prev_close_price")
        hi_c = cols.get("highest52weeks_price") or cols.get("52_week_high")
        lo_c = cols.get("lowest52weeks_price") or cols.get("52_week_low")

        rows = []
        for _, row in snap.iterrows():
            code = str(row[code_c]) if code_c else ""
            meta = code_meta.get(code, {})
            name = meta.get("name") or (str(row[name_c]) if name_c else code)
            ptype = monitor._classify(name, code)
            if ptype in ("窝轮", "杠杆ETF"):
                continue
            if not _tradable_from_row(row, cols):
                continue
            price = _num(row[price_c]) if price_c else None
            chg = _num(row[chg_c]) if chg_c else None
            prev = _num(row[prev_c]) if prev_c else None
            hi = _num(row[hi_c]) if hi_c else None
            lo = _num(row[lo_c]) if lo_c else None
            if price is None or price <= 0:
                continue
            if chg is None and prev:
                chg = round((price - prev) / prev * 100, 2)
            chg = chg or 0.0
            pos_pct = None
            if hi and lo and hi > lo:
                pos_pct = (price - lo) / (hi - lo) * 100
            rows.append({
                "code": code, "name": name,
                "source": meta.get("source", "龙头"),
                "price": round(price, 3),
                "change_rate": chg,
                "week52_position_pct": round(pos_pct, 1) if pos_pct is not None else None,
            })

        # 3) 跟跌筛选：今日下跌的候选
        down = [r for r in rows if r["change_rate"] < 0]
        down.sort(key=lambda x: x["change_rate"])  # 跌最多的在前
        down_codes = [r["code"] for r in down][:REVERSE_CAP]

        # 4) 反向信号批量打分（仅跟跌候选，控制 API 压力）
        rev_map: Dict[str, Tuple[Optional[dict], Optional[str]]] = {}
        if down_codes:
            try:
                rev_map = reverse_signals.reverse_score_batch(
                    client, down_codes, days=60, num=10)
            except Exception as e:  # noqa: BLE001
                errors.append(f"反向信号打分失败：{e}")
                rev_map = {}

        for r in down:
            rev, _ = rev_map.get(r["code"], (None, None))
            rev_score = float((rev or {}).get("score", 0.0) or 0.0)
            rev_signals = (rev or {}).get("signals", []) or []
            pos_pct = r["week52_position_pct"]
            # 深度超跌加成
            depth_bonus = 0.0
            if pos_pct is not None:
                if pos_pct <= 20:
                    depth_bonus = 3.0
                elif pos_pct <= 40:
                    depth_bonus = 2.0
                elif pos_pct <= 60:
                    depth_bonus = 1.0
            # 跟跌幅度加成
            drop_bonus = 0.0
            if r["change_rate"] <= -5:
                drop_bonus = 1.5
            elif r["change_rate"] <= -3:
                drop_bonus = 1.0
            dip_score = round(rev_score + depth_bonus + drop_bonus, 1)
            r["reverse_score"] = round(rev_score, 1)
            r["reverse_signals"] = rev_signals
            r["depth_bonus"] = depth_bonus
            r["drop_bonus"] = drop_bonus
            r["dip_score"] = dip_score
            r["advice"] = _advice(crash, rev_score, pos_pct, r["change_rate"])
            r["conviction"] = _conviction(dip_score)
            candidates.append(r)

        candidates.sort(key=lambda x: x["dip_score"], reverse=True)

    markdown = _build_markdown(idx, crash, threshold, candidates)
    return {
        "ok": True,
        "index": idx,
        "threshold": threshold,
        "crash": crash,
        "candidates": candidates,
        "markdown": markdown,
        "errors": errors,
    }


def _conviction(dip_score: float) -> str:
    if dip_score >= 6:
        return "强"
    if dip_score >= 3:
        return "中"
    if dip_score > 0:
        return "弱"
    return "观望"


def _advice(crash: bool, rev_score: float, pos_pct, chg: float) -> str:
    if not crash:
        return "恒科未急跌，个股跟跌多为正常波动，暂不加仓，维持观察。"
    if rev_score >= 6 and pos_pct is not None and pos_pct <= 30:
        return "指数急跌错杀 + 个股反向信号强（低位于52周低位），可分批低吸。"
    if rev_score >= 3:
        return "指数急跌跟跌，反向信号偏多，小仓试探，等企稳再加。"
    if rev_score > 0:
        return "指数急跌跟跌，有微弱反向信号，轻仓观察。"
    return "跟随大盘跟跌，个股无独立利好，观望为主。"


def _build_markdown(idx: dict, crash: bool, threshold: float,
                    candidates: List[dict]) -> str:
    """生成企业微信 markdown（受 4096 字节保护）。"""
    cr = idx.get("change_rate")
    cr_txt = f"{cr:+.2f}%" if cr is not None else "—"
    dd = idx.get("intraday_drawdown")
    dd_txt = f"{dd:+.2f}%" if dd is not None else "—"
    status = "🟢 急跌（触发低吸扫描）" if crash else "⚪ 常态"
    lines = [
        f"**📡 盘中急跌联动扫描**",
        f"恒生科技指数({idx.get('code')}) 现价{idx.get('last_price') or '—'} "
        f"涨跌 {cr_txt}（日内回撤 {dd_txt}） → {status}",
        "",
    ]
    if idx.get("error"):
        lines.append(f"（指数数据获取失败：{idx['error']}，按常规扫描）")
        lines.append("")

    if not candidates:
        lines.append("当前候选池无「跟跌」个股，或数据获取失败，无低吸信号。")
        return "\n".join(lines).rstrip("\n")

    top = candidates[:TOP_PUSH]
    hit = [c for c in top if c["dip_score"] > 0]
    lines.append(f"跟跌候选 {len(candidates)} 只，低吸信号 {len(hit)} 只（按低吸吸引力排序）：")
    lines.append("")

    def _fits(block: str) -> bool:
        return len("\n".join(lines).encode("utf-8")) + len(block.encode("utf-8")) <= MAX_BYTES

    shown = 0
    for i, c in enumerate(top, 1):
        sigs = "、".join(c["reverse_signals"]) or "—"
        block = (
            f"{i}. **{c['name']}({c['code']})** [{c['source']}] 现价{c['price']} "
            f"今日{c['change_rate']:+.2f}% 52周位{c['week52_position_pct'] if c['week52_position_pct'] is not None else '—'}% "
            f"反向{c['reverse_score']} 低吸{c['dip_score']}({c['conviction']})\n"
            f"   信号：{sigs}\n"
            f"   建议：{c['advice']}\n---"
        )
        if _fits(block):
            lines.append(block.rstrip("\n"))
            shown += 1
        else:
            break
    if shown < len(top):
        lines.append(f"（其余 {len(top) - shown} 只省略）")
    if candidates:
        lines.append("")
        lines.append(HSTECH_EVIDENCE)
    return "\n".join(lines).rstrip("\n")


def run_intraday(client, webhook: str = "", cfg: Optional[dict] = None,
                 codes: Optional[List[str]] = None,
                 code_meta: Optional[Dict[str, dict]] = None) -> dict:
    """执行一次盘中扫描，并在「急跌触发」时推送企业微信（带指纹去重）。

    返回 scan_linkage 结果 + pushed 标志。
    """
    cfg = cfg or {}
    res = scan_linkage(
        client,
        hstech_code=cfg.get("hstech_code", HSTECH_DEFAULT),
        threshold=cfg.get("threshold", THRESHOLD_DEFAULT),
        codes=codes, code_meta=code_meta,
    )
    pushed = False
    if res.get("crash") and webhook:
        # 指纹按「日期 + 指数 + Top3 候选」生成，避免同一急跌窗口内重复轰炸；
        # 候选集明显变化（换仓）才会重新推送，最短间隔 30 分钟。
        top3 = "|".join(c["code"] for c in res["candidates"][:3])
        fp = f"intraday:{datetime.now():%Y%m%d}:{res['index'].get('code')}:{top3}"
        pushed = notify.push_if_new(fp, res["markdown"], webhook, min_interval=1800)
    res["pushed"] = pushed
    return res

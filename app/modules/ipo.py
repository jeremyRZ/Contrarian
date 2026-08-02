"""
新股打新模块 - 港股打新专用 skill 完整移植
- 5 维度打分（0-10，加权汇总）：市场情绪30% / 基本面20% / 稀缺性23% / 估值12% / 配售15%
- 综合打分 + 一句话说明 + 申购建议
- 中签率预测：具名档位（甲头/甲中/甲大/甲尾/大甲尾；乙头/乙中/乙尾/顶头锤）+ 5 个真实校准案例
- 招股信息完整字段（核心数据速览）
- 联网获取（新浪 best-effort，失败降级手动输入）
- 完整打新分析报告输出（结构化 + Markdown）
"""
from __future__ import annotations

import re

import requests

WEIGHTS = {
    "market_sentiment": 0.30,
    "fundamentals": 0.20,
    "rarity": 0.23,
    "valuation": 0.12,
    "placement": 0.15,
}

DIM_LABELS = {
    "market_sentiment": "市场情绪",
    "fundamentals": "基本面",
    "rarity": "稀缺性",
    "valuation": "估值合理度",
    "placement": "配售结构",
}

# 中签率校准案例库（基于公开信息整理，预测仅供参考）
CALIBRATED_CASES = [
    {"name": "蜜雪冰城", "code": "02197.HK", "sub_times": 5125, "a_tail": "~70%", "b_head": "~30%", "hammer": "~261%"},
    {"name": "茶百道", "code": "02555.HK", "sub_times": 2000, "a_tail": "~30-40%", "b_head": "~15-20%"},
    {"name": "古茗", "code": "0136.HK", "sub_times": 445, "a_tail": "~50-60%", "b_head": "~40-50%"},
    {"name": "巨星传奇", "code": "6683.HK", "sub_times": 979, "a_tail": "~40-50%", "b_head": "~30-40%"},
    {"name": "布鲁可", "code": "02383.HK", "sub_times": 2275, "a_tail": "~20-30%", "b_head": "~15-20%", "hammer": "~100%+"},
]


# ---------------- 5 维度打分（与 skill 阶梯一致） ----------------

def score_market_sentiment(sub_times: float) -> float:
    if sub_times >= 5000:
        return 9.5
    if sub_times >= 2000:
        return 8.0
    if sub_times >= 500:
        return 6.0
    if sub_times >= 100:
        return 4.0
    return 1.5


def score_fundamentals(revenue: float, profitable: bool) -> float:
    # revenue 单位：亿港元
    if revenue >= 50 and profitable:
        return 9.0
    if revenue >= 50 and not profitable:
        return 7.0
    if revenue >= 10:
        return 6.0
    if revenue >= 1:
        return 4.0
    return 1.5


def score_rarity(tier: str) -> float:
    return {
        "global_unique": 9.0,
        "top3": 7.0,
        "front": 4.5,
        "red_ocean": 1.5,
    }.get(tier, 4.5)


def score_valuation(level: str) -> float:
    return {
        "discount": 9.0,
        "slight_low": 7.0,
        "fair": 5.0,
        "slight_high": 3.0,
        "high": 1.0,
    }.get(level, 5.0)


def score_placement(cornerstone_pct: float, top_sponsor: bool) -> float:
    if cornerstone_pct >= 50 and top_sponsor:
        return 9.0
    if cornerstone_pct >= 30:
        return 6.0
    if cornerstone_pct >= 15:
        return 4.0
    return 1.5


# ---------------- 维度一句话说明 ----------------

def _remark_sentiment(sub_times: float) -> str:
    if sub_times >= 5000:
        return "历史级极度热门（≥5000倍认购），市场情绪爆棚"
    if sub_times >= 2000:
        return "非常热门（2000-4999倍），资金追捧"
    if sub_times >= 500:
        return "较为热门（500-1999倍），关注度高"
    if sub_times >= 100:
        return "一般（100-499倍），情绪中性"
    return "冷淡（<100倍），认购意愿弱"


def _remark_fundamentals(revenue: float, profitable: bool) -> str:
    if revenue >= 50 and profitable:
        return "营收>50亿且盈利，基本面扎实"
    if revenue >= 50 and not profitable:
        return "营收>50亿但亏损收窄，规模大待盈利"
    if revenue >= 10:
        return "营收10-50亿，盈利/减亏中"
    if revenue >= 1:
        return "营收1-10亿，亏损收窄阶段"
    return "营收<1亿且持续亏损，基本面偏弱"


def _remark_rarity(tier: str) -> str:
    return {
        "global_unique": "全球/中国唯一，行业空白，稀缺性极高",
        "top3": "赛道前三，有差异化壁垒",
        "front": "赛道前列，无明显护城河",
        "red_ocean": "红海竞争，替代品多",
    }.get(tier, "赛道前列，无明显护城河")


def _remark_valuation(level: str) -> str:
    return {
        "discount": "折价发行（较行业低30%+），估值吸引",
        "slight_low": "略低于行业均值，估值合理偏低",
        "fair": "与行业均值持平，估值中性",
        "slight_high": "略高于行业均值，估值略贵",
        "high": "明显偏高（高50%+），估值承压",
    }.get(level, "估值中性")


def _remark_placement(cornerstone_pct: float, top_sponsor: bool) -> str:
    if cornerstone_pct >= 50 and top_sponsor:
        return "基石>50%且顶级保荐人，配售结构优"
    if cornerstone_pct >= 30:
        return "基石30-50%，一线保荐人护航"
    if cornerstone_pct >= 15:
        return "基石15-30%，二线保荐人"
    return "基石<15%或保荐人一般，配售偏弱"


# ---------------- 中签率预测 ----------------

def _band_of(sub_times: float) -> str:
    if sub_times >= 5000:
        return ">=5000"
    if sub_times >= 2000:
        return "2000-4999"
    if sub_times >= 500:
        return "500-1999"
    return "<500"


def predict_allotment(sub_times: float) -> dict:
    band = _band_of(sub_times)
    group_a = {
        "甲头": {"range": "0.5-2%", "2000-4999": "1-5%", "500-1999": "5-15%", "<500": "15-30%"},
        "甲中": {"range": "2-8%", "2000-4999": "5-15%", "500-1999": "15-35%", "<500": "35-60%"},
        "甲大": {"range": "5-15%", "2000-4999": "10-25%", "500-1999": "25-45%", "<500": "50-70%"},
        "甲尾": {"range": "20-45%", "2000-4999": "15-35%", "500-1999": "25-45%", "<500": "—"},
        "大甲尾": {"range": "30-60%", "2000-4999": "25-45%", "500-1999": "—", "<500": "—"},
    }
    group_b = {
        "乙头": {"range": "10-25%", "2000-4999": "20-35%", "500-1999": "35-55%", "<500": "60-80%"},
        "乙中": {"range": "15-30%", "2000-4999": "25-40%", "500-1999": "40-60%", "<500": "70-90%"},
        "乙尾": {"range": "30-50%", "2000-4999": "35-55%", "500-1999": "50-70%", "<500": "—"},
        "顶头锤": {"range": "60-100%+", "2000-4999": "50-80%", "500-1999": "—", "<500": "—"},
    }
    # 校准案例：取认购倍数最接近的作为参照
    calibration = None
    if sub_times > 0:
        best = min(CALIBRATED_CASES, key=lambda c: abs(c["sub_times"] - sub_times))
        calibration = {k: best[k] for k in ("name", "code", "sub_times", "a_tail", "b_head", "hammer") if k in best}

    return {
        "band": band,
        "group_a": {k: (v["range"] if band == ">=5000" else v.get(band, "—")) for k, v in group_a.items()},
        "group_b": {k: (v["range"] if band == ">=5000" else v.get(band, "—")) for k, v in group_b.items()},
        "calibration": calibration,
    }


# ---------------- 主分析 ----------------

def analyze_ipo(data: dict):
    """
    data 字段（均可选，缺失则取保守默认）：
      name, code, price_low, price_high, lot_size, entry_fee,
      sub_start, sub_end, list_date, sponsors, cornerstone_pct,
      sub_times, revenue, profitable, rarity_tier, valuation_level, top_sponsor
    返回 (result_dict, error)
    """
    sub_times = float(data.get("sub_times") or 0)
    revenue = float(data.get("revenue") or 0)
    profitable = bool(data.get("profitable", False))
    rarity = data.get("rarity_tier", "front")
    val_level = data.get("valuation_level", "fair")
    corner = float(data.get("cornerstone_pct") or 0)
    top_sp = bool(data.get("top_sponsor", False))

    dims = {
        "market_sentiment": score_market_sentiment(sub_times),
        "fundamentals": score_fundamentals(revenue, profitable),
        "rarity": score_rarity(rarity),
        "valuation": score_valuation(val_level),
        "placement": score_placement(corner, top_sp),
    }
    composite = round(sum(dims[k] * WEIGHTS[k] for k in WEIGHTS), 1)

    verdict = ("积极申购" if composite >= 7 else
               "中性偏积极" if composite >= 5.5 else
               "谨慎/回避" if composite >= 4 else "回避")

    allot = predict_allotment(sub_times)

    prospectus = {
        "price_low": data.get("price_low"),
        "price_high": data.get("price_high"),
        "lot_size": data.get("lot_size"),
        "entry_fee": data.get("entry_fee"),
        "sub_start": data.get("sub_start"),
        "sub_end": data.get("sub_end"),
        "list_date": data.get("list_date"),
        "sponsors": data.get("sponsors"),
        "cornerstone_pct": data.get("cornerstone_pct"),
    }

    remarks = {
        "market_sentiment": _remark_sentiment(sub_times),
        "fundamentals": _remark_fundamentals(revenue, profitable),
        "rarity": _remark_rarity(rarity),
        "valuation": _remark_valuation(val_level),
        "placement": _remark_placement(corner, top_sp),
    }

    # 数据完整性
    required = ["sub_times", "revenue", "cornerstone_pct", "price_low", "sponsors"]
    missing = [k for k in required if not data.get(k)]
    completeness = ("完整" if not missing
                    else f"部分数据缺失（{', '.join(missing)}），以下基于已知字段估算，实际结果可能有所偏差")

    report_md = _build_report_md(data, dims, composite, verdict, prospectus, remarks, allot)

    result = {
        "name": data.get("name", ""),
        "code": data.get("code", ""),
        "composite_score": composite,
        "verdict": verdict,
        "dimensions": {k: round(v, 1) for k, v in dims.items()},
        "dimensions_remark": remarks,
        "weights": WEIGHTS,
        "prospectus": prospectus,
        "allotment": allot,
        "report_md": report_md,
        "completeness": completeness,
        "missing_fields": missing,
        "sub_times": sub_times,
    }
    return result, None


def _fmt(v, suffix: str = "") -> str:
    if v is None or v == "":
        return "—"
    return f"{v}{suffix}"


def _build_report_md(data, dims, composite, verdict, prospectus, remarks, allot) -> str:
    p = prospectus
    price = f"{_fmt(p['price_low'])} - {_fmt(p['price_high'])}" if (p['price_low'] or p['price_high']) else "—"
    lines = []
    lines.append(f"## {data.get('name', '')}（{data.get('code', '')}）打新分析\n")
    lines.append("### 核心数据速览")
    lines.append("| 项目 | 数据 |")
    lines.append("|------|------|")
    lines.append(f"| 招股价 | {price} |")
    lines.append(f"| 每手股数 | {_fmt(p['lot_size'])} |")
    lines.append(f"| 每手入场费 | {_fmt(p['entry_fee'])} |")
    lines.append(f"| 认购日期 | {_fmt(p['sub_start'])} ~ {_fmt(p['sub_end'])} |")
    lines.append(f"| 上市日期 | {_fmt(p['list_date'])} |")
    lines.append(f"| 保荐人 | {_fmt(p['sponsors'])} |")
    lines.append(f"| 基石投资者 | {_fmt(p['cornerstone_pct'], '%')} |")
    lines.append("")
    lines.append("### 市场情绪")
    lines.append(f"- 认购倍数：{_fmt(data.get('sub_times'))} 倍（热度：{_remark_sentiment(float(data.get('sub_times') or 0)).split('（')[0]}）")
    lines.append(f"- {remarks['market_sentiment']}")
    lines.append("")
    lines.append("### 公司基本面")
    lines.append(f"- {remarks['fundamentals']}")
    lines.append("")
    lines.append("### 综合打分")
    lines.append(f"**综合打分：{composite}/10**")
    for k in WEIGHTS:
        lines.append(f"- {DIM_LABELS[k]} {dims[k]:.1f}/10：{remarks[k]}")
    lines.append("")
    lines.append("### 中签率预测")
    if allot.get("calibration"):
        c = allot["calibration"]
        lines.append(f"> 校准参照：{c['name']}（{c['code']}）认购 {c['sub_times']} 倍，"
                     f"甲尾 {c.get('a_tail', '—')}，乙头 {c.get('b_head', '—')}"
                     + (f"，顶头锤 {c['hammer']}" if 'hammer' in c else ""))
    ga = allot["group_a"]; gb = allot["group_b"]
    lines.append("| 档位 | 甲组 | 乙组 |")
    lines.append("|------|------|------|")
    keys_a = list(ga.keys()); keys_b = list(gb.keys())
    for i, ka in enumerate(keys_a):
        kb = keys_b[i] if i < len(keys_b) else ""
        lines.append(f"| {ka} | {ga[ka]} | {gb.get(kb, '—') if kb else '—'} |")
    lines.append("")
    lines.append("### 综合建议")
    lines.append(f"**{verdict}**（综合 {composite}/10）。{remarks['rarity']}；{remarks['valuation']}；{remarks['placement']}。"
                 f"甲尾资金效率通常高于乙头，同等热度下中签率往往更高。")
    return "\n".join(lines)


# ---------------- 联网获取（best-effort） ----------------

def _code_num(code: str) -> str:
    return code.replace("HK.", "").replace(".HK", "").strip()


def fetch_ipo_info(code: str, timeout: int = 15):
    """
    优先联网获取招股信息（新浪 best-effort），返回 dict 或 None（不阻塞）。
    解析字段：招股价、每手股数、入场费、保荐人、基石比例。
    """
    try:
        num = _code_num(code)
        # 新浪港股新股/行情页（best-effort）
        urls = [
            f"https://finance.sina.com.cn/stock/hkstock/quote/{num}.shtml",
            f"https://stock.finance.sina.com.cn/hkstock/api/jsonp.php/var%20HK_{num}=",
        ]
        out = {}
        for url in urls:
            try:
                r = requests.get(url, timeout=timeout, headers={"User-Agent": "Mozilla/5.0"})
            except Exception:  # noqa: BLE001
                continue
            if r.status_code != 200:
                continue
            html = r.text
            # 招股价 / 发行价
            m = re.search(r"(?:发行价|招股价)[^\d]{0,6}([\d.]+)", html)
            if m and "price_low" not in out:
                out["price_low"] = float(m.group(1))
            # 每手股数
            m = re.search(r"每手[^\d]{0,6}(\d+)\s*股", html)
            if m and "lot_size" not in out:
                out["lot_size"] = int(m.group(1))
            # 入场费
            m = re.search(r"入场费[^\d]{0,6}([\d,]+)", html)
            if m and "entry_fee" not in out:
                out["entry_fee"] = float(m.group(1).replace(",", ""))
            # 保荐人
            m = re.search(r"保荐[人商][^\u4e00-\u9fff]{0,4}([\u4e00-\u9fff]{2,10})", html)
            if m and "sponsors" not in out:
                out["sponsors"] = m.group(1)
            if out:
                break
        return out or None
    except Exception:  # noqa: BLE001
        return None


def auto_analyze(code: str, timeout: int = 15):
    """联网取招股信息后自动分析；取数失败返回 (None, 错误信息)。"""
    info = fetch_ipo_info(code, timeout=timeout)
    if not info:
        return None, "联网获取招股信息失败，请手动输入后打分"
    data = {"code": code, **info}
    return analyze_ipo(data)

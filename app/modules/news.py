"""个股新闻（富途 OpenAPI）+ 细粒度情绪打分。

normalize 富途 get_search_news 返回的 DataFrame（title/source/publish_time/
url/news_sub_type/view_count），统一为 {title, time, src, url, type, views} 列表，
并对每条标题做中文金融情绪打分，输出 sentiment（标签）+ sentiment_score（浮点）。

情绪升级（v1.6.0）：
1) 词典法增强：扩充否定词（辟谣/否认/澄清/传闻不实）+ 反讽短语处理，改善复杂句式
   与反讽识别（如「否认减持」「澄清无暴雷」应判中性而非利空）。
2) 可选轻量 LLM 复核：若 config.news.llm 配置了 base_url/api_key，则对每条标题调用
   OpenAI 兼容接口做情绪复核；无配置时自动降级到词典法（默认行为，零成本）。
"""
from __future__ import annotations

import json
import urllib.request
from typing import Optional, Tuple

from ..cache import cached
from ..futu_client import load_config


_CFG = None


def _cfg() -> dict:
    global _CFG
    if _CFG is None:
        _CFG = load_config()
    return _CFG


def _num(v):
    try:
        if v is None:
            return None
        f = float(v)
        return None if f != f else f
    except (ValueError, TypeError):
        return None


# 细粒度情绪词典：权重绝对值代表强度（1~2 强，0.5~1 中）。
POS = {
    "利好": 2.0, "回购": 1.5, "增持": 1.5, "上调": 1.5, "买入": 1.2, "盈利": 1.2,
    "增长": 1.2, "中标": 1.2, "合作": 1.0, "获批": 1.3, "大涨": 1.5, "突破": 1.0,
    "新高": 1.2, "超预期": 1.8, "分红": 1.0, "扩产": 1.0, "扭亏": 1.5, "营收": 0.6,
    "净利润": 0.8, "创新高": 1.2, "签约": 1.0, "中标金额": 1.2, "提价": 1.0,
    "获配": 1.0, "增持计划": 1.6, "回购计划": 1.6, "业绩预增": 1.8, "订单": 1.1,
    "特别股息": 1.6, "注销": 1.0, "分拆上市": 1.0, "盈利预警": 1.4, "上调指引": 1.5,
}
NEG = {
    "减持": -1.5, "下调": -1.5, "卖出": -1.2, "亏损": -1.3, "诉讼": -1.2, "处罚": -1.3,
    "大跌": -1.5, "风险": -1.0, "警示": -1.2, "降级": -1.5, "违约": -1.8, "暴雷": -2.0,
    "停产": -1.3, "推迟": -1.0, "下滑": -1.2, "预减": -1.5, "退市": -2.0, "质疑": -1.0,
    "净利润下滑": -1.3, "业绩预减": -1.8, "被罚": -1.4, "立案": -1.6, "商誉减值": -1.6,
    "减持计划": -1.6, "减持股份": -1.5, "暴跌": -1.6, "黑天鹅": -1.8,
    "被砍": -1.3, "砍": -1.0, "告吹": -1.3, "流产": -1.3, "腰斩": -1.5, "撤资": -1.5,
    "终止合作": -1.4, "取消合作": -1.4, "终止": -1.2, "取消": -1.0, "暴跌": -1.6,
    "盈利警告": -1.6, "下调指引": -1.5, "业绩变脸": -1.8, "做空报告": -1.6,
}
# 否定 / 澄清词：出现在其后关键词前（窗口内）则翻转极性。
# 扩充了「辟谣 / 否认 / 澄清 / 传闻不实」等，改善反讽与辟谣标题识别。
NEGATION = ["不", "未", "无", "没", "否", "非", "暂未", "尚未", "并未", "并非", "未能",
            "不再", "解除", "终止", "取消", "撤销", "叫停", "辟谣", "否认", "澄清",
            "传闻", "不实", "误读", "系误读", "回应称", "否认称", "不存在"]
# 程度词：放大其后关键词权重
INTENS = {"大幅": 1.5, "大举": 1.5, "显著": 1.3, "持续": 1.2, "连续": 1.1, "急剧": 1.6,
          "明显": 1.2, "预计": 1.0, "可能": 0.8, "或将": 0.9, "小幅": 0.6, "微": 0.5,
          "拟": 0.7, "计划": 0.7}
# 反讽短语：整句若出现「传闻」「网传」+ 否定澄清，倾向于中性（靠 NEGATION 处理）；
# 这里额外列出「被指在」「质疑」等需结合上下文，仅作弱信号。


def score_sentiment(title: str) -> Tuple[float, str]:
    """对单条标题做细粒度情绪打分（词典法）。

    返回 (score, label)。score ∈ [-3, 3]；label ∈
    {强烈利好, 利好, 中性, 利空, 强烈利空}。
    处理：否定/澄清词翻转极性、程度词放大权重、多关键词累加。
    """
    t = str(title or "")
    if not t:
        return 0.0, "中性"
    s = 0.0
    for kw, w in list(POS.items()) + list(NEG.items()):
        idx = t.find(kw)
        while idx != -1:
            pre = t[max(0, idx - 6):idx]
            neg = any(n in pre for n in NEGATION)
            mult = 1.0
            for ik, iw in INTENS.items():
                if ik in pre:
                    mult = max(mult, iw)
            s += w * mult * (-1.0 if neg else 1.0)
            nxt = t.find(kw, idx + len(kw))
            if nxt == idx:
                break
            idx = nxt
    s = max(-3.0, min(3.0, s))
    if s >= 1.5:
        lab = "强烈利好"
    elif s >= 0.4:
        lab = "利好"
    elif s > -0.4:
        lab = "中性"
    elif s > -1.5:
        lab = "利空"
    else:
        lab = "强烈利空"
    return round(s, 2), lab


def llm_review(title: str) -> Optional[Tuple[float, str]]:
    """可选轻量 LLM 情绪复核（OpenAI 兼容接口）。无配置或失败返回 None。"""
    sec = _cfg().get("news", {})
    llm = sec.get("llm") or {}
    base_url = llm.get("base_url")
    api_key = llm.get("api_key")
    if not base_url or not api_key:
        return None
    try:
        body = {
            "model": llm.get("model", "gpt-4o-mini"),
            "messages": [
                {"role": "system", "content": (
                    "你是港股金融新闻情绪分析器。判断给定新闻标题的市场情绪：利好 / 利空 / 中性，"
                    "并给出 -3 到 3 的分数（正数为利好，负数为利空，0 为中性，考虑反讽与辟谣）。"
                    "只返回 JSON：{\"label\":\"利好|利空|中性\",\"score\":浮点数,\"reason\":\"简短理由\"}")},
                {"role": "user", "content": title},
            ],
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }
        req = urllib.request.Request(
            base_url, data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + api_key},
        )
        with urllib.request.urlopen(req, timeout=8) as r:
            j = json.loads(r.read())
        content = j["choices"][0]["message"]["content"]
        o = json.loads(content)
        sc = float(o.get("score", 0))
        lab = str(o.get("label", "中性"))
        return max(-3.0, min(3.0, sc)), lab
    except Exception:  # noqa: BLE001
        return None


@cached(skip_first=True)
def get_news(client, code: str, num: int = 10) -> Tuple[Optional[dict], Optional[str]]:
    raw, err = client.news(code, num=num)
    if err:
        return None, err
    if raw is None:
        return {"code": code, "news": []}, None
    rows = []
    try:
        if isinstance(raw, list):
            rows = raw
        elif hasattr(raw, "to_dict"):
            rows = raw.to_dict("records")
    except Exception:  # noqa: BLE001
        rows = []
    items = []
    for it in rows[:num]:
        if not isinstance(it, dict):
            continue
        title = str(it.get("title", "") or "")
        # 优先 LLM 复核，失败/未配置则词典法
        rev = llm_review(title)
        if rev is not None:
            sc, lab = rev
        else:
            sc, lab = score_sentiment(title)
        items.append({
            "title": title,
            "time": str(it.get("publish_time", "") or it.get("pub_time", "") or it.get("time", "") or ""),
            "src": str(it.get("source", "") or it.get("src", "") or ""),
            "url": str(it.get("url", "") or ""),
            "type": str(it.get("news_sub_type", "") or it.get("type", "") or ""),
            "views": _num(it.get("view_count")),
            "sentiment": lab,
            "sentiment_score": round(sc, 2),
            "llm": rev is not None,
        })
    return {"code": code, "news": items}, None

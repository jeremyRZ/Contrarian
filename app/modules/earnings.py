"""财报窗口期信号（业绩预告 / 公告前后的错杀机会窗口）。

数据源策略：
- 主：财报季月份启发式（无需外部数据，恒可用）。港股业绩集中披露期：
  年报季 3~4 月、中报季 8~9 月。处于披露季即标记「财报季」窗口。
- 辅：可选东财业绩日历（datacenter-web RPT_LICO_FN_CPD）获取精确下次业绩日，
  计算距今天数，±window_days 内标记「精确财报窗口」。该源对港股常返回空，
  故仅作增强，缺失时退回月份启发式。

信号（错杀反向加分）：
  精确窗口(±14日) → +0.5「财报窗口·错杀机会」
  财报季(月份)     → +0.2「财报季」
  无数据           → 0（available=False，不计入）
"""
from __future__ import annotations

import datetime
import json
import urllib.parse
import urllib.request
from typing import Optional, Tuple

SEASON_MONTHS = (3, 4, 8, 9)          # 年报季 / 中报季
WINDOW_DAYS = 14                       # 精确窗口前后天数


def _eastmoney_next_date(code: str) -> Optional[str]:
    """尝试从东财业绩日历取下次业绩日（YYYY-MM-DD）。港股常为空，返回 None。"""
    try:
        flt = f'(SECURITY_CODE="{code}")'
        q = "&".join(f"{k}={urllib.parse.quote(str(v))}" for k, v in {
            "reportName": "RPT_LICO_FN_CPD", "columns": "ALL",
            "source": "WEB", "client": "WEB", "pageSize": "5",
            "sortColumns": "REPORTDATE", "sortTypes": "-1", "filter": flt,
        }.items())
        url = "https://datacenter-web.eastmoney.com/api/data/v1/get?" + q
        req = urllib.request.Request(
            url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://data.eastmoney.com/"})
        with urllib.request.urlopen(req, timeout=6) as r:
            j = json.loads(r.read())
        rows = (j.get("result") or {}).get("data") or []
        today = datetime.date.today()
        cand = []
        for it in rows:
            rd = it.get("NOTICE_DATE") or it.get("REPORTDATE")
            if not rd:
                continue
            try:
                d = datetime.datetime.strptime(str(rd)[:10], "%Y-%m-%d").date()
            except Exception:  # noqa: BLE001
                continue
            if d >= today:
                cand.append(d)
        if cand:
            return min(cand).isoformat()
    except Exception:  # noqa: BLE001
        return None
    return None


def earnings_signal(code: str) -> Tuple[Optional[dict], Optional[str]]:
    """计算单票财报窗口信号。返回 (dict, error)。error 恒为 None（优雅降级）。"""
    today = datetime.date.today()
    month = today.month
    in_season = month in SEASON_MONTHS

    next_date = _eastmoney_next_date(code)
    days_to = None
    in_window = False
    if next_date:
        try:
            nd = datetime.datetime.strptime(next_date[:10], "%Y-%m-%d").date()
            days_to = (nd - today).days
            in_window = abs(days_to) <= WINDOW_DAYS
        except Exception:  # noqa: BLE001
            days_to = None

    score = 0.0
    label = None
    if in_window:
        score, label = 0.5, "财报窗口·错杀机会"
    elif in_season:
        score, label = 0.2, "财报季"

    return {
        "next_date": next_date,
        "days_to": days_to,
        "in_season": in_season,
        "in_window": in_window,
        "available": next_date is not None,
        "score": round(score, 1),
        "label": label,
    }, None

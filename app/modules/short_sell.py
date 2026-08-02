"""港股沽空(卖空)反向信号（第 7 档）。

数据源：东方财富港股沽空记录页 `hk.eastmoney.com/sellshort_N.html`（服务端渲染表格，
含逐日 沽空占成交比例 / 沽空金额 / 总成交金额）。该页按时间窗分页：
  sellshort_1 = 最近 50 条（最新），sellshort_2..5 = 更早窗口。
实测最新数据约滞后数周（港交所披露经东财归档，非实时），故返回 data_date + stale_days，
调用方/前端需显式标注「数据截至 YYYY-MM-DD」，避免用户误以为是实时值。

⚠ 富途 OpenAPI 的 get_daily_short_volume / get_short_interest 对港股均返回空表
（列是美股模板），故本档必走东财源。

信号逻辑（错杀猎手 / 逆向）：
  沽空占成交比(ratio) 是看跌情绪最直接的「流量」指标。
  - 高且仍在升 → 做空压力加大 = 错杀反向利空（风险）
  - 极高但已开始回落 → 空头回补/挤仓前兆 = 温和利好
  仅作风险提示，权重保守；数据滞后 >30 天则减半并标注。
所有函数返回 (data, error)；error 非 None 时上层优雅展示 / 计 0，不抛异常。
"""
from __future__ import annotations

import re
import urllib.request
from datetime import date, datetime
from typing import Optional, Tuple

from ..cache import cached

_HOST = "https://hk.eastmoney.com"
_HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://hk.eastmoney.com/"}


def _hk_code(code: str) -> str:
    c = str(code or "").strip()
    if "." in c:
        c = c.split(".", 1)[1]
    return c.strip()


def _http_get(url: str, timeout: int = 12) -> str:
    req = urllib.request.Request(url, headers=_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "ignore")


def _cell_text(cell: str) -> str:
    return re.sub(r"<[^>]+>", "", cell).strip()


def _parse_page(html: str) -> list:
    """从单个 sellshort_N 页面解析出逐日沽空记录。

    页面表格列：序号 / 代码 / 名称 / 最新价 / 沽空数量 / 沽空均价 / 沽空金额 /
    总成交金额 / 沽空占比(%) / 日期。代码/名称可能在 <span> 或 <a> 内，故按
    <td> 单元格取文本，再定位「含%的占比」与「YYYY-MM-DD 的日期」，避免布局变动误判。
    """
    tb = html.find("<tbody>")
    te = html.find("</tbody>")
    if tb < 0 or te < 0 or te <= tb:
        return []
    rows = re.findall(r"<tr>(.*?)</tr>", html[tb:te], re.S)
    out = []
    for r in rows:
        cells = re.findall(r"<td[^>]*>(.*?)</td>", r, re.S)
        texts = [_cell_text(c) for c in cells]
        ratio = None
        d = None
        for t in texts:
            if ratio is None and "%" in t:
                try:
                    ratio = float(t.replace("%", "").replace(",", "").strip())
                except (ValueError, TypeError):
                    ratio = None
            if d is None and re.match(r"^\d{4}-\d{2}-\d{2}$", t.strip()):
                d = t.strip()
        if ratio is not None and d:
            out.append({"date": d, "ratio": ratio})
    return out


@cached()
def short_sell_series(code: str, pages: int = 2) -> Tuple[Optional[list], Optional[str]]:
    """逐日沽空占比序列（升序，去重）。返回 (list[{date,ratio,...}], error)。"""
    hk = _hk_code(code)
    if not hk:
        return None, "未提供有效港股代码"
    try:
        allrows = []
        seen = set()
        for tab in range(1, max(1, pages) + 1):
            url = f"{_HOST}/sellshort_{tab}.html?code={hk}&sdate=&edate="
            try:
                for row in _parse_page(_http_get(url)):
                    if row["date"] not in seen:
                        seen.add(row["date"])
                        allrows.append(row)
            except Exception:  # noqa: BLE001
                continue
        if not allrows:
            return None, f"未找到 {code} 的沽空数据（可能非卖空标的）"
        allrows.sort(key=lambda x: x["date"])
        return allrows, None
    except Exception as ex:  # noqa: BLE001
        return None, f"沽空数据获取失败: {ex}"


@cached()
def short_sell_signal(code: str) -> Tuple[Optional[dict], Optional[str]]:
    """沽空反向信号。返回 (dict, error)。

    dict: {latest_ratio, avg20, avg5, delta, data_date, stale_days, stale,
           series(近30), score, label}
    """
    series, err = short_sell_series(code, pages=2)
    if err:
        return None, err
    if not series:
        return None, "无沽空序列"

    latest = series[-1]
    ratio = float(latest["ratio"])
    ratios = [float(r["ratio"]) for r in series]
    n = len(ratios)
    avg20 = sum(ratios[-20:]) / min(20, n)
    avg5 = sum(ratios[-5:]) / min(5, n)
    delta = ratio - avg20

    try:
        dd = datetime.strptime(str(latest["date"])[:10], "%Y-%m-%d").date()
        stale = (date.today() - dd).days
    except Exception:
        stale = None

    # 评分（逆向、保守）
    score = 0.0
    label = None
    if ratio >= 25 and delta > 3:
        score, label = -1.5, "沽空占比高且仍在升(做空压力)"
    elif ratio >= 20 and delta > 1:
        score, label = -1.0, "沽空占比偏高(做空活跃)"
    elif delta > 3:
        score, label = -0.5, "沽空占比近期抬升"
    elif ratio <= 15 and delta < -3:
        score, label = 0.5, "沽空占比回落(空头回补前兆)"
    elif ratio <= 12 and delta < -5:
        score, label = 0.5, "沽空占比快速回落"

    stale_flag = (stale is not None and stale > 30)
    if stale_flag and score != 0.0:
        score = round(score * 0.5, 1)
        if label:
            label = label + "(数据滞后)"

    return {
        "latest_ratio": round(ratio, 2),
        "avg20": round(avg20, 2),
        "avg5": round(avg5, 2),
        "delta": round(delta, 2),
        "data_date": str(latest["date"])[:10],
        "stale_days": stale,
        "stale": stale_flag,
        "series": series[-30:],
        "score": round(score, 1),
        "label": label,
    }, None

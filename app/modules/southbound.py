"""南向资金（港股通）数据。

- 市场级净流向/余额：东方财富 push2his（无需登录，已验证可用）
- 个股港股通持股：东方财富沪深港通持股明细
  RPT_MUTUAL_STOCK_HOLDRANKS（INTERVAL_TYPE=1 即南向），已实测可返回
  个股的持股数量/占比/当日增减/连续增持天数

所有函数都返回 (data, error)；error 非 None 时上层应优雅展示，不抛异常。
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Optional, Tuple

from ..cache import cached


def _num(v):
    try:
        if v is None or v == "":
            return None
        f = float(v)
        return None if f != f else f
    except (ValueError, TypeError):
        return None


def _http_get(url: str, params: dict, timeout: int = 8) -> str:
    q = "&".join(f"{k}={urllib.parse.quote(str(v))}" for k, v in params.items())
    req = urllib.request.Request(
        url + "?" + q,
        headers={"User-Agent": "Mozilla/5.0", "Referer": "https://data.eastmoney.com/"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "ignore")


def _http_json(url: str, params: dict, timeout: int = 10) -> dict:
    raw = _http_get(url, params, timeout=timeout)
    s, e = raw.find("{"), raw.rfind("}")
    return json.loads(raw[s:e + 1])


def _hk_code(code: str) -> str:
    """从富途代码(HK.00700)或纯数字(00700)提取港股 5 位代码（保留前导 0）。"""
    c = str(code or "").strip()
    if "." in c:
        c = c.split(".", 1)[1]
    return c.strip()


@cached()
def market_netflow(days: int = 30) -> Tuple[Optional[dict], Optional[str]]:
    """南向(港股通)每日成交净买额序列（真实数据）。

    数据源：东方财富沪深港通历史数据 `RPT_MUTUAL_DEAL_HISTORY`
    （MUTUAL_TYPE="006" 即南向），与东方财富官网「沪深港通资金流向」页面同源。
    字段 NET_DEAL_AMT 单位为百万元，÷100 得亿元。

    直接调用东方财富接口，**不依赖 akshare**，避免未装 akshare 时掉入
    推送接口的「额度余额」假数据。获取失败时返回诚实错误，绝不返回伪造数值。

    返回 {source, latest:{date,value}, delta5, series:[{date,value}], note}
    （value 单位：亿元；正=净买入，负=净卖出）
    """
    try:
        params = {
            "sortColumns": "TRADE_DATE", "sortTypes": "-1",
            "pageSize": str(max(int(days), 5)), "pageNumber": "1",
            "reportName": "RPT_MUTUAL_DEAL_HISTORY",
            "columns": "ALL", "source": "WEB", "client": "WEB",
            "filter": '(MUTUAL_TYPE="006")',
        }
        d = _http_json("https://datacenter-web.eastmoney.com/api/data/v1/get", params)
        rows = (d.get("result") or {}).get("data") or []
        if not rows:
            return None, "南向资金历史数据为空（可能非交易日或接口限流）"

        series = []
        for r in rows:
            try:
                # NET_DEAL_AMT 单位：百万元 → ÷100 = 亿元
                v = float(r.get("NET_DEAL_AMT")) / 100.0
            except (ValueError, TypeError):
                v = None
            series.append({"date": str(r.get("TRADE_DATE"))[:10], "value": v})

        # 接口按日期降序返回，转成升序，latest 在末尾
        series.sort(key=lambda x: x["date"])
        series = series[-max(int(days), 1):]

        latest = series[-1]
        if latest["value"] is not None:
            latest = {**latest, "value": round(latest["value"], 2)}
        delta5 = None
        if len(series) >= 6 and latest["value"] is not None and series[-6]["value"] is not None:
            delta5 = round(latest["value"] - series[-6]["value"], 2)
        return {
            "source": "eastmoney-hsgt",
            "latest": latest,
            "delta5": delta5,
            "series": series,
            "note": "南向(港股通)每日成交净买额，单位：亿元（东方财富沪深港通历史数据，真实值）",
        }, None
    except Exception as ex:  # noqa: BLE001
        return None, f"南向资金获取失败: {ex}"


@cached()
def holding(code: str) -> Tuple[Optional[dict], Optional[str]]:
    """个股港股通(南向)持股。

    数据源：东方财富沪深港通持股明细 RPT_MUTUAL_STOCK_HOLDRANKS
    （INTERVAL_TYPE=1 即南向、MUTUAL_TYPE=002）。实测可返回个股的
    持股数量、占发行股比、当日增减、近 5 日连续增持天数等。
    返回 (dict, None) 成功；(None, error) 失败。
    """
    hk = _hk_code(code)
    if not hk:
        return None, "未提供有效港股代码"
    try:
        params = {
            "sortColumns": "TRADE_DATE", "sortTypes": "-1",
            "pageSize": "5", "pageNumber": "1",
            "columns": "ALL", "source": "WEB", "client": "WEB",
            "filter": f'(INTERVAL_TYPE="1")(RN=1)(SECURITY_CODE="{hk}")',
            "reportName": "RPT_MUTUAL_STOCK_HOLDRANKS",
        }
        d = _http_json("https://datacenter-web.eastmoney.com/api/data/v1/get", params)
        rows = (d.get("result") or {}).get("data") or []
        if not rows:
            return None, f"未找到 {code} 的港股通持股数据（可能非港股通标的）"

        latest = rows[0]

        def g(k):
            v = latest.get(k)
            return None if v in (None, "") else v

        # 连续增持 / 减持天数：从最新一日往前，按同一方向连续计数，符号反转即停
        up = 0
        down = 0
        run_sign = 0
        for r in rows:
            try:
                chg = float(r.get("HOLD_SHARES_CHANGE") or 0)
            except (ValueError, TypeError):
                chg = 0.0
            s = 1 if chg > 0 else (-1 if chg < 0 else 0)
            if s == 0:
                break
            if run_sign == 0:
                run_sign = s
            if s != run_sign:
                break
            if s > 0:
                up += 1
            else:
                down += 1

        hold_shares = _num(g("HOLD_SHARES"))
        chg_shares = _num(g("HOLD_SHARES_CHANGE"))
        prev_shares = (hold_shares - chg_shares) if (hold_shares is not None and chg_shares is not None) else None
        chg_ratio = round(chg_shares / prev_shares * 100, 3) if (chg_shares is not None and prev_shares) else None

        # 反向风险警示：连续减持 / 单日骤减
        risk = []
        if down >= 3:
            risk.append(f"南向连续减持{down}日")
        if chg_ratio is not None and chg_ratio <= -5.0:
            risk.append(f"单日南向骤减{abs(chg_ratio):.1f}%")

        return {
            "code": code,
            "hk_code": hk,
            "name": g("SECURITY_NAME"),
            "date": str(g("HOLD_DATE") or g("TRADE_DATE") or "")[:10],
            "hold_shares": hold_shares,
            "hold_ratio": round(_num(g("HOLD_SHARES_RATIO")) or 0, 2),       # 占发行股比 %
            "hold_market_cap": _num(g("HOLD_MARKET_CAP")),
            "chg_shares_1d": chg_shares,
            "chg_market_cap_1d": _num(g("ADD_MARKET_CAP")),
            "chg_ratio_1d": chg_ratio,
            "total_shares_ratio": round(_num(g("TOTAL_SHARES_RATIO")) or 0, 2),
            "close": _num(g("CLOSE_PRICE")),
            "change_rate": _num(g("CHANGE_RATE")),
            "industry": g("INDUSTRY"),
            "contiguous_up_days": up,
            "contiguous_down_days": down,
            "risk": risk,
            "source": "eastmoney-hsgt",
        }, None
    except Exception as ex:  # noqa: BLE001
        return None, f"港股通持股获取失败: {ex}"

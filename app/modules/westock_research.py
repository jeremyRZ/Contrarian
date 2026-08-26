"""Structured HK research enrichment with Futu as the production source.

Futu OpenD supplies financial statements and analyst consensus. Eastmoney
supplies Stock Connect holdings. The optional westock CLI is retained only as a
fallback for notices and older installations. Provider no-coverage, failures,
and cached stale data remain distinct states.
"""
from __future__ import annotations

import glob
import os
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pandas as pd

from . import southbound

_CACHE: dict[str, tuple[float, dict]] = {}
_LOCK = threading.Lock()
_TTL = 6 * 3600


def _cli() -> tuple[str | None, str | None]:
    node = glob.glob(os.path.expandvars(
        r"%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe"
    ))
    scripts = glob.glob(os.path.expandvars(
        r"%LOCALAPPDATA%\pnpm-cache\dlx\*\*\node_modules\westock-data-skillhub\index.js"
    ))
    return (node[0] if node else None, max(scripts, key=os.path.getmtime) if scripts else None)


def _run(args: list[str]) -> dict:
    node, script = _cli()
    if not node or not script:
        return {"status": "unavailable", "error": "本机结构化投研数据组件未安装"}
    try:
        cp = subprocess.run([node, script, *args], capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=35)
        out = (cp.stdout or "").strip()
        if cp.returncode or "执行失败" in out:
            return {"status": "no_coverage" if "未找到" in out else "failed",
                    "error": out or cp.stderr.strip() or "数据源查询失败"}
        return {"status": "available", "raw": out}
    except subprocess.TimeoutExpired:
        return {"status": "failed", "error": "结构化数据源查询超时"}
    except Exception as exc:  # noqa: BLE001
        return {"status": "failed", "error": f"结构化数据源异常: {exc}"}


def _rows(raw: str) -> list[dict]:
    lines = [x.strip() for x in raw.splitlines() if x.strip().startswith("|")]
    result, headers = [], None
    for line in lines:
        cells = [x.strip() for x in line.strip("|").split("|")]
        if all(set(x) <= {"-", ":"} for x in cells):
            continue
        if headers is None:
            headers = cells
        elif len(cells) == len(headers):
            result.append(dict(zip(headers, cells)))
    return result


def _number(value):
    try:
        return float(value) if value not in (None, "", "-") else None
    except (TypeError, ValueError):
        return None


def _financial_periods(payload: dict | None) -> list[dict]:
    """Normalize Futu F10 income statement field IDs into UI-facing periods."""
    reports = (payload or {}).get("report_list") or []
    result = []
    for report in reports[:4]:
        items = {int(x.get("field_id")): x for x in report.get("item_list", [])
                 if x.get("field_id") is not None}
        revenue = _number((items.get(5001) or items.get(5002) or {}).get("data"))
        gross_profit = _number((items.get(5010) or {}).get("data"))
        operating_profit = _number((items.get(5034) or {}).get("data"))
        parent_profit = _number((items.get(5051) or items.get(5045) or {}).get("data"))
        if revenue is None and parent_profit is None:
            continue
        result.append({
            "date": report.get("date_time_str"),
            "report_type": report.get("period_text"),
            "currency": report.get("currency_code"),
            "revenue": revenue,
            "revenue_yoy": _number((items.get(5001) or items.get(5002) or {}).get("yoy")),
            "net_profit": parent_profit,
            "net_profit_yoy": _number((items.get(5051) or items.get(5045) or {}).get("yoy")),
            "gross_margin": (round(gross_profit / revenue * 100, 2)
                             if gross_profit is not None and revenue else None),
            "operating_margin": (round(operating_profit / revenue * 100, 2)
                                 if operating_profit is not None and revenue else None),
            "operating_cashflow": None,
        })
    return result


def _snapshot_price(client, code: str):
    frame, _ = client.market_snapshot([code])
    if frame is None or not isinstance(frame, pd.DataFrame) or frame.empty:
        return None
    return _number(frame.iloc[0].get("last_price"))


def _futu_research(client, code: str) -> dict:
    finance_payload, finance_error = client.financial_statements(code, num=8)
    periods = _financial_periods(finance_payload)
    finance = ({"status": "available", "source": "futu-openapi-f10",
                "as_of": periods[0].get("date"), "periods": periods}
               if periods else
               {"status": "failed" if finance_error else "no_coverage",
                "error": finance_error or "富途未覆盖该股财务报表"})

    consensus_payload, consensus_error = client.analyst_consensus(code)
    if consensus_payload and _number(consensus_payload.get("total")):
        total = int(_number(consensus_payload.get("total")) or 0)
        bullish_pct = ((_number(consensus_payload.get("strong_buy")) or 0) +
                       (_number(consensus_payload.get("buy")) or 0))
        hold_pct = _number(consensus_payload.get("hold")) or 0
        buy_count = round(total * bullish_pct / 100)
        hold_count = round(total * hold_pct / 100)
        sell_count = max(total - buy_count - hold_count, 0)
        update_date = consensus_payload.get("update_time_str")
        current_price = _snapshot_price(client, code)
        rating = {
            "status": "available", "source": "futu-openapi-consensus", "as_of": update_date,
            "summary": {
                "forecastInstitutions": total,
                "targetPriceAvg": _number(consensus_payload.get("average")),
                "currentPrice": current_price,
                "ratingBuyCnt": buy_count,
                "ratingHoldCnt": hold_count,
                "ratingSellCnt": sell_count,
                "bullishPct": round(bullish_pct, 2),
            },
        }
        consensus = {
            "status": "available", "source": "futu-openapi-consensus", "as_of": update_date,
            "summary": {
                "institutions": total,
                "targetPriceLow": _number(consensus_payload.get("lowest")),
                "targetPriceAvg": _number(consensus_payload.get("average")),
                "targetPriceHigh": _number(consensus_payload.get("highest")),
                "bullishPct": round(bullish_pct, 2),
                "holdPct": round(hold_pct, 2),
            },
        }
    else:
        state = "failed" if consensus_error else "no_coverage"
        error = consensus_error or "富途未覆盖该股分析师一致预期"
        rating = {"status": state, "error": error}
        consensus = {"status": state, "error": error}
    return {"finance": finance, "rating": rating, "consensus": consensus}


def _southbound_research(code: str) -> dict:
    data, error = southbound.holding(code)
    if data:
        return {"status": "available", "source": data.get("source"),
                "stale": bool(data.get("stale")),
                "summary": {"date": data.get("date"),
                            "holding_ratio": data.get("hold_ratio"),
                            "holding_shares": data.get("hold_shares"),
                            "day_change_shares": data.get("chg_shares_1d")}}
    no_coverage = bool(error and ("未找到" in error or "非港股通" in error))
    return {"status": "no_coverage" if no_coverage else "failed",
            "error": error or "南向持股数据不可用"}


def _summarize(kind: str, result: dict) -> dict:
    if result.get("status") != "available":
        return result
    rows = _rows(result.get("raw", ""))
    if kind == "south" and rows:
        r = rows[0]
        result["summary"] = {"date": r.get("code") and result["raw"].split("（", 1)[-1].split("）", 1)[0],
                             "holding_ratio": _number(r.get("持有比例(%)")),
                             "holding_shares": _number(r.get("持股数量")),
                             "day_change_shares": _number(r.get("日变动份额")),
                             "quarter_change_shares": _number(r.get("季变动份额"))}
    elif kind == "rating" and rows:
        r = next((x for x in rows if "targetPriceAvg" in x), rows[0])
        result["summary"] = {k: _number(r.get(k)) for k in
                             ("forecastInstitutions", "targetPriceAvg", "currentPrice",
                              "ratingBuyCnt", "ratingIncCnt", "ratingHoldCnt", "ratingSellCnt")}
    elif kind == "finance" and rows:
        useful = [x for x in rows if "OperatingIncome" in x]
        result["periods"] = [{"date": x.get("date"), "report_type": x.get("ReportType"),
                              "revenue": _number(x.get("OperatingIncome")),
                              "revenue_yoy": _number(x.get("OperatingRevenueGr1y")),
                              "net_profit": _number(x.get("ProfitToShareholders")),
                              "net_profit_yoy": _number(x.get("NpParentCompanyGr1y")),
                              "gross_margin": _number(x.get("GrossIncomeRatio")),
                              "operating_margin": _number(x.get("OperatingProfitRatio")),
                              "operating_cashflow": _number(x.get("CFO"))}
                             for x in useful[:4]]
        if not result["periods"]:
            result.update(status="failed", error="财务数据返回格式无法识别")
    elif kind == "notices":
        result["items"] = [{"title": x.get("title"), "time": x.get("time")} for x in rows[:10]]
    result.pop("raw", None)
    return result


def get_research(code: str, client=None) -> dict:
    normalized = "hk" + "".join(ch for ch in code if ch.isdigit()).zfill(5)
    with _LOCK:
        cached = _CACHE.get(normalized)
        if cached and time.time() - cached[0] < _TTL:
            return cached[1]
    data = _futu_research(client, code) if client is not None else {}
    commands = {"notices": ["notice", "list", normalized, "--limit", "10"]}
    for kind in ("finance", "rating", "consensus"):
        if (data.get(kind) or {}).get("status") != "available":
            commands[kind] = (["finance", normalized, "--num", "4"] if kind == "finance"
                              else [kind, normalized])
    with ThreadPoolExecutor(max_workers=len(commands)) as pool:
        futures = {k: pool.submit(_run, v) for k, v in commands.items()}
        for kind, future in futures.items():
            fallback = _summarize(kind, future.result())
            if kind not in data or data[kind].get("status") != "available":
                data[kind] = fallback
    data["south"] = _southbound_research(code)
    data["broker_reports"] = {"status": "unsupported",
                              "error": "当前结构化研报接口仅覆盖A股；港股以公告和机构评级作为替代依据"}
    with _LOCK:
        _CACHE[normalized] = (time.time(), data)
    return data

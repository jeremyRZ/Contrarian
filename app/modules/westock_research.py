"""Structured HK research enrichment through the locally installed westock CLI.

The adapter deliberately distinguishes available data, provider no-coverage, and
transport failures. Results are cached in memory so one page request does not
repeatedly hit the upstream service.
"""
from __future__ import annotations

import glob
import os
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor

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


def get_research(code: str) -> dict:
    normalized = "hk" + "".join(ch for ch in code if ch.isdigit()).zfill(5)
    with _LOCK:
        cached = _CACHE.get(normalized)
        if cached and time.time() - cached[0] < _TTL:
            return cached[1]
    commands = {
        "finance": ["finance", normalized, "--num", "4"],
        "rating": ["rating", normalized],
        "consensus": ["consensus", normalized],
        "south": ["fund", "south-holding", normalized],
        "notices": ["notice", "list", normalized, "--limit", "10"],
    }
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {k: pool.submit(_run, v) for k, v in commands.items()}
        data = {k: _summarize(k, f.result()) for k, f in futures.items()}
    data["broker_reports"] = {"status": "unsupported",
                              "error": "当前结构化研报接口仅覆盖A股；港股以公告和机构评级作为替代依据"}
    with _LOCK:
        _CACHE[normalized] = (time.time(), data)
    return data

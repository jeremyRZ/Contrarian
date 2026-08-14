from __future__ import annotations

import re

from .models import Security

_MARKETS = {
    "HK": ("HKEX", "HKD"),
    "SH": ("SSE", "CNY"),
    "SZ": ("SZSE", "CNY"),
    "BJ": ("BSE", "CNY"),
    "US": ("US", "USD"),
}


def normalize_code(value: str, market: str | None = None) -> str:
    raw = str(value or "").strip().upper()
    if not raw:
        raise ValueError("证券代码不能为空")
    raw = raw.replace(".SS", "").replace(".SZ", "") if raw.endswith((".SS", ".SZ")) else raw
    if "." in raw:
        prefix, symbol = raw.split(".", 1)
        if prefix not in _MARKETS or not symbol:
            raise ValueError(f"不支持的证券代码: {value}")
        return f"{prefix}.{symbol}"
    hint = str(market or "").upper()
    if re.fullmatch(r"\d{6}", raw):
        prefix = hint if hint in {"SH", "SZ", "BJ"} else (
            "BJ" if raw[0] in "48" else ("SH" if raw[0] in "569" else "SZ"))
        return f"{prefix}.{raw}"
    if re.fullmatch(r"\d{5}", raw) and hint in {"HK", ""}:
        return f"HK.{raw}"
    if re.fullmatch(r"[A-Z][A-Z0-9.-]{0,14}", raw) and hint in {"US", ""}:
        return f"US.{raw}"
    raise ValueError(f"无法识别证券代码: {value}")


def resolve_security(value: str, market: str | None = None, name: str | None = None) -> Security:
    code = normalize_code(value, market)
    prefix, symbol = code.split(".", 1)
    exchange, currency = _MARKETS[prefix]
    logical_market = "CN" if prefix in {"SH", "SZ", "BJ"} else prefix
    return Security(code, symbol, logical_market, exchange, currency, name=name)

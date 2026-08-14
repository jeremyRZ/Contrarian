from __future__ import annotations

from pathlib import Path

import pandas as pd

from app.markets.registry import normalize_code, resolve_security

ROOT = Path(__file__).resolve().parents[2]


def _normalise_bars(frame: pd.DataFrame, code: str) -> pd.DataFrame:
    x = frame.copy()
    rename = {}
    for col in x.columns:
        low = str(col).lower()
        if low in {"date", "datetime", "time", "time_key"}: rename[col] = "date"
        elif low in {"open", "high", "low", "close", "volume", "turnover", "amount"}: rename[col] = low
    x = x.rename(columns=rename)
    required = {"date", "open", "high", "low", "close", "volume"}
    missing = required.difference(x.columns)
    if missing: raise ValueError("K线缺少字段: " + ", ".join(sorted(missing)))
    x["date"] = pd.to_datetime(x["date"])
    for col in required - {"date"}:
        x[col] = pd.to_numeric(x[col], errors="coerce")
    x["code"] = code
    return x.sort_values("date").drop_duplicates("date", keep="last").dropna(subset=["close"])


class MarketDataRouter:
    """Market-neutral facade. Futu is primary; local research cache is fallback."""

    def __init__(self, futu_client, tiger_provider=None):
        self.client = futu_client
        self.tiger = tiger_provider

    def snapshot(self, codes: list[str]):
        normalized = [normalize_code(c) for c in codes]
        return self.client.market_snapshot(normalized)

    def daily_bars(self, code: str, *, start: str | None = None,
                   end: str | None = None, count: int = 500):
        normalized = normalize_code(code)
        cache = ROOT / ".runtime" / (normalized.lower().replace(".", "") + "_qfq_daily.csv")
        # Research requests need long histories and should not block on a quote
        # entitlement check when a reproducible local cache already exists.
        if count >= 500 and cache.exists():
            try:
                x = _normalise_bars(pd.read_csv(cache), normalized)
                if start: x = x[x.date >= pd.Timestamp(start)]
                if end: x = x[x.date <= pd.Timestamp(end)]
                return x.tail(count), None
            except Exception:  # noqa: BLE001 - live provider may still recover
                pass
        frame, err = self.client.history_kline(normalized, max_count=count, start=start, end=end)
        if not err and frame is not None and not frame.empty:
            try: return _normalise_bars(frame, normalized), None
            except ValueError as exc: return None, str(exc)
        if cache.exists():
            try:
                x = _normalise_bars(pd.read_csv(cache), normalized)
                if start: x = x[x.date >= pd.Timestamp(start)]
                if end: x = x[x.date <= pd.Timestamp(end)]
                return x.tail(count), None
            except Exception as exc:  # noqa: BLE001
                return None, f"本地K线缓存不可读: {exc}"
        return None, err or f"{normalized} 无可用K线数据"

    def securities(self, market: str):
        key = str(market).upper()
        if key == "CN":
            parts, errors = [], []
            try:
                import futu as ft
                targets = (ft.Market.SH, ft.Market.SZ)
            except Exception:  # pragma: no cover
                targets = ("SH", "SZ")
            for target in targets:
                try: frame, err = self.client.stock_basicinfo(market=target)
                except TypeError: frame, err = None, "客户端不支持按市场查询证券列表"
                if err: errors.append(str(err))
                elif frame is not None and not frame.empty: parts.append(frame)
            if parts: return pd.concat(parts, ignore_index=True).drop_duplicates("code"), None
            return None, "; ".join(errors) or "A股证券列表不可用"
        try: return self.client.stock_basicinfo(), None
        except Exception as exc: return None, str(exc)

    def search(self, query: str, market: str = "CN", limit: int = 20):
        frame, err = self.securities(market)
        if err or frame is None: return [], err
        q = str(query or "").strip().lower()
        code_col = next((c for c in frame.columns if str(c).lower() == "code"), None)
        name_col = next((c for c in frame.columns if str(c).lower() in {"name", "stock_name"}), None)
        if not code_col: return [], "证券列表缺少code字段"
        if q:
            mask = frame[code_col].astype(str).str.lower().str.contains(q, regex=False)
            if name_col: mask |= frame[name_col].astype(str).str.lower().str.contains(q, regex=False)
            frame = frame[mask]
        rows = []
        for _, row in frame.head(max(1, min(limit, 100))).iterrows():
            try: sec = resolve_security(str(row[code_col]), name=str(row[name_col]) if name_col else None)
            except ValueError: continue
            rows.append(sec.to_dict())
        return rows, None

    def positions(self, market: str | None = None):
        key = str(market or "").upper()
        if key == "US" and self.tiger is not None:
            return self.tiger.positions()
        if market and hasattr(self.client, "positions_market"):
            frame, err = self.client.positions_market(str(market).upper())
        else:
            frame, err = self.client.positions()
        if err or frame is None or frame.empty: return frame, err
        if not market: return frame, None
        key = str(market).upper()
        codes = frame["code"].astype(str)
        mask = codes.str.startswith(("SH.", "SZ.")) if key == "CN" else codes.str.startswith(key + ".")
        return frame[mask].copy(), None

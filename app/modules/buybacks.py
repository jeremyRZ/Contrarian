"""个股回购数据（富途 OpenAPI）。

normalize 富途 get_corporate_actions_buybacks 返回的 hk_buy_back_list，
转成前端友好的结构。失败时由上层 _wrap 统一处理。
"""
from __future__ import annotations

from typing import Optional, Tuple

from ..cache import cached


def _num(v):
    try:
        if v is None:
            return None
        f = float(v)
        return None if f != f else f  # NaN -> None
    except (ValueError, TypeError):
        return None


@cached(skip_first=True)
def get_buybacks(client, code: str, num: int = 10) -> Tuple[Optional[dict], Optional[str]]:
    raw, err = client.buybacks(code, num=num)
    if err:
        return None, err
    if not raw:
        return {"code": code, "buybacks": []}, None
    df = raw.get("hk_buy_back_list")
    if df is None or (hasattr(df, "empty") and df.empty):
        return {"code": code, "buybacks": []}, None
    items = []
    for _, r in df.iterrows():
        items.append({
            "date": str(r.get("publ_date_str") or r.get("publ_date") or ""),
            "amount": _num(r.get("buy_back_money")),
            "shares": _num(r.get("buy_back_sum")),
            "pct": round(_num(r.get("percentage")) or 0, 2),
            "cum_shares": _num(r.get("cumulative_sum")),
            "cum_pct": round(_num(r.get("cumulative_percentage")) or 0, 2),
            "high": _num(r.get("high_price")),
            "low": _num(r.get("low_price")),
        })
    return {"code": code, "buybacks": items}, None

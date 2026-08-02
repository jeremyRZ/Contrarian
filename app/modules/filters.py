"""标的交易性过滤（自动剔除停牌 / 无报价 / 无估值的标的）。

将「手动排除中国绿宝」的做法通用化：用富途 get_market_snapshot 检测
suspension（停牌标志）、last_price（无报价→0）、pe/pb（无估值→0）。
持仓快捷选择、综合选股、错杀扫描、南向风险聚合等候选池都应先过此过滤，
避免把停牌老千股 / 仙股噪声混入分析。
"""
from __future__ import annotations

from typing import Optional, Tuple

from ..cache import cached


@cached(skip_first=True)
def snapshot_dict(client, code: str) -> Optional[dict]:
    """拉取单票行情快照，归一化为 dict（含 suspension/last_price/pe_ratio/
    pb_ratio/dividend_* 等字段）。失败返回 None。"""
    try:
        snap, err = client.market_snapshot([code])
    except Exception:  # noqa: BLE001
        return None
    if err or snap is None or snap.empty:
        return None
    cols = {c.lower(): c for c in snap.columns}
    code_c = cols.get("code")
    row = snap[snap[code_c].astype(str) == str(code)]
    if row.empty:
        return None
    r = row.iloc[0]
    out = {}
    for k in ["suspension", "last_price", "prev_close_price", "sec_status",
              "equity_valid", "pe_ratio", "pb_ratio",
              "dividend_ttm", "dividend_ratio_ttm", "dividend_lfy", "dividend_lfy_ratio",
              "name"]:
        out[k] = r.get(k)
    return out


def is_tradable(client, code: str) -> Tuple[bool, str]:
    """判断标的是否正常可交易（有报价、有估值、未停牌）。

    返回 (bool, reason)。reason 仅在不可交易时有意义。
    """
    d = snapshot_dict(client, code)
    if d is None:
        return False, "行情获取失败"
    susp = d.get("suspension")
    lp = d.get("last_price")
    pe = d.get("pe_ratio")
    pb = d.get("pb_ratio")
    try:
        lp = float(lp) if lp is not None else 0.0
    except (ValueError, TypeError):
        lp = 0.0
    try:
        pe = float(pe) if pe is not None else 0.0
    except (ValueError, TypeError):
        pe = 0.0
    try:
        pb = float(pb) if pb is not None else 0.0
    except (ValueError, TypeError):
        pb = 0.0
    if susp is True:
        return False, "停牌"
    if lp <= 0:
        return False, "无报价（last_price=0）"
    if pe <= 0 and pb <= 0:
        return False, "无估值（pe/pb=0，可能已退市/停牌）"
    return True, ""


def filter_tradable(client, codes: list) -> dict:
    """批量过滤。返回 {'tradable':[code...], 'excluded':[{code,reason}...]}。"""
    tradable, excluded = [], []
    for code in codes:
        ok, reason = is_tradable(client, code)
        if ok:
            tradable.append(code)
        else:
            excluded.append({"code": code, "reason": reason})
    return {"tradable": tradable, "excluded": excluded}

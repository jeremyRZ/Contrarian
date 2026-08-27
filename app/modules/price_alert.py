"""
个股价格多级报警模块
- 三级下跌报警：warn 预警(减仓) -> alarm 警告 -> stop 触发止损
- 止盈目标 tp 到达提醒
- 日内异常跌幅检测（change_rate <= -5%）
- 不重复轰炸：同一代码每个级别仅首次推送，升级才推新消息（state 记录已触发信号）
- 配置驱动（config.price_alert.list）+ 运行时可增（POST /price-alerts）
"""
from __future__ import annotations

from typing import Optional
from ..numbers import as_float as _num

# 运行时报警列表（POST 追加，进程内有效）
RUNTIME_LIST: list[dict] = []
# code -> 已触发信号 key 集合（用于去重，避免重复轰炸）
_FIRED: dict[str, set] = {}


def add_runtime(entry: dict) -> dict:
    """校验并追加一条运行时报警配置。返回规范化后的条目。"""
    item = {
        "code": str(entry.get("code", "")).strip(),
        "name": str(entry.get("name", "")).strip() or entry.get("code", ""),
        "warn_px": _num(entry.get("warn_px")) or 0.0,
        "alarm_px": _num(entry.get("alarm_px")) or 0.0,
        "stop_px": _num(entry.get("stop_px")) or 0.0,
        "tp_px": _num(entry.get("tp_px")) or 0.0,
        "note": str(entry.get("note", "")).strip(),
    }
    RUNTIME_LIST.append(item)
    return item


def list_config(config: dict) -> list:
    cfg_list = (config.get("price_alert", {}) or {}).get("list", []) or []
    merged = [dict(x) for x in cfg_list] + [dict(x) for x in RUNTIME_LIST]
    return merged


def _evaluate(code: str, name: str, price: float, change_rate: float, cfg: dict,
              mark_fired: bool = True):
    """返回该代码当前应触发的信号列表 [(key, level, msg)]。"""
    fired = _FIRED.setdefault(code, set()) if mark_fired else _FIRED.get(code, set())
    triggered = []
    note = cfg.get("note") or ""
    def _msg(base: str) -> str:
        return f"{base}{('｜' + note) if note else ''}"
    # 止盈目标（向上）
    if cfg.get("tp_px") and price >= cfg["tp_px"]:
        triggered.append((f"{code}:tp", "tp", _msg(f"到达止盈目标 {cfg['tp_px']}，现价 {price}")))
    # 止损（向下，最高优先级）
    if cfg.get("stop_px") and price <= cfg["stop_px"]:
        triggered.append((f"{code}:stop", "stop", _msg(f"跌破止损价 {cfg['stop_px']}，现价 {price}")))
    elif cfg.get("alarm_px") and price <= cfg["alarm_px"]:
        triggered.append((f"{code}:alarm", "alarm", _msg(f"触及警告价 {cfg['alarm_px']}，现价 {price}")))
    elif cfg.get("warn_px") and price <= cfg["warn_px"]:
        triggered.append((f"{code}:warn", "warn", _msg(f"到达预警价 {cfg['warn_px']}，现价 {price}")))
    # 日内异常跌幅
    if (change_rate or 0) <= -5:
        triggered.append((f"{code}:drop", "alarm", f"日内异常跌幅 {change_rate}%"))
    # 去重：仅返回未推送过的
    fresh = [t for t in triggered if t[0] not in fired]
    if mark_fired:
        for t in fresh:
            fired.add(t[0])
    return fresh, triggered


def evaluate_all(client, config: dict, mark_fired: bool = True):
    """
    检查所有报警配置的当前价格状态。返回 (dict, error)。
    dict: {items[], alerts_to_push[], count, push_count}
    """
    cfg_list = list_config(config)
    if not cfg_list:
        return {"items": [], "alerts_to_push": [], "count": 0, "push_count": 0}, None
    codes = [c["code"] for c in cfg_list]
    snap, err = client.market_snapshot(codes)
    if err:
        return None, err
    if snap is None or snap.empty:
        return None, "快照获取失败"
    cols = {c.lower(): c for c in snap.columns}
    code_c = cols.get("code")
    price_c = cols.get("last_price")
    chg_c = cols.get("change_rate")
    prev_c = cols.get("prev_close_price")
    price_map = {}
    for _, row in snap.iterrows():
        c = str(row[code_c])
        price = _num(row[price_c])
        chg = _num(row[chg_c]) if chg_c else None
        if chg is None and price and prev_c:
            prev = _num(row[prev_c])
            chg = round((price - prev) / prev * 100, 2) if prev else 0.0
        price_map[c] = (price, chg or 0.0)

    items = []
    alerts_to_push = []
    for cfg in cfg_list:
        code = cfg["code"]
        price, chg = price_map.get(code, (None, 0.0))
        if price is None:
            items.append({"code": code, "name": cfg.get("name", code),
                          "price": None, "change_rate": None,
                          "active_signals": [], "would_push": False, "note": "无行情"})
            continue
        fresh, all_trig = _evaluate(
            code, cfg.get("name", code), price, chg, cfg, mark_fired=mark_fired
        )
        active = [t[2] for t in all_trig]
        would_push = bool(fresh)
        for t in fresh:
            alerts_to_push.append({"code": code, "name": cfg.get("name", code),
                                    "level": t[1], "msg": t[2]})
        items.append({
            "code": code, "name": cfg.get("name", code),
            "price": price, "change_rate": chg,
            "warn_px": cfg.get("warn_px") or None,
            "alarm_px": cfg.get("alarm_px") or None,
            "stop_px": cfg.get("stop_px") or None,
            "tp_px": cfg.get("tp_px") or None,
            "note": cfg.get("note") or "",
            "active_signals": active,
            "would_push": would_push,
        })
    return {
        "items": items,
        "alerts_to_push": alerts_to_push,
        "count": len(items),
        "push_count": len(alerts_to_push),
    }, None

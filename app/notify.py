"""
企业微信群机器人预警推送
- push_wecom(text, webhook): 以 markdown 消息推送到群机器人 webhook
- push_if_new(fingerprint, text, webhook): 带指纹去重 + 频率限制，避免重复轰炸
- notify_alerts / notify_missed: 持仓预警 / 错杀观察 汇总推送
- 无 webhook 时降级为服务端日志（不报错、不阻塞主流程）
"""
from __future__ import annotations

import logging
import time

import requests

from . import local_notify
from .modules import notification_ledger

logger = logging.getLogger("hk-notify")

# fingerprint -> 上次成功推送的时间戳
_LAST_PUSH: dict[str, float] = {}
_DEFAULT_INTERVAL = 600  # 同指纹最短推送间隔（秒）


def _send_wecom(text: str, webhook: str = "", timeout: int = 5) -> tuple[bool, str]:
    """Low-level sender. Audit is owned by the public notification functions."""
    if not webhook:
        logger.warning("[WeCom-降级] %s", text.replace("\n", " | "))
        return False, "WEBHOOK_NOT_CONFIGURED"
    try:
        r = requests.post(webhook, json={"msgtype": "markdown", "markdown": {"content": text}},
                          timeout=timeout)
        if r.status_code == 200:
            return True, "HTTP_200"
        logger.error("[WeCom-失败] HTTP %s: %s", r.status_code, r.text[:200])
        return False, f"HTTP_{r.status_code}"
    except Exception as exc:  # noqa: BLE001
        logger.error("[WeCom-异常] %s", exc)
        return False, type(exc).__name__


def push_wecom(text: str, webhook: str = "", timeout: int = 5) -> bool:
    """推送 markdown 消息到企业微信群机器人。无 webhook 时降级为日志，返回是否真正推送。"""
    fingerprint = f"direct:{hash(text)}"
    ok, detail = _send_wecom(text, webhook, timeout)
    notification_ledger.record(fingerprint, text, "SENT" if ok else "FAILED", detail=detail)
    return ok


def push_if_new(fingerprint: str, text: str, webhook: str = "",
                min_interval: int = _DEFAULT_INTERVAL, *, title: str = "Contrarian交易提醒") -> bool:
    """指纹去重推送：同一指纹在 min_interval 内只推一次，避免重复轰炸。"""
    now = time.time()
    last = _LAST_PUSH.get(fingerprint)
    if last and (now - last) < min_interval:
        logger.info("[WeCom-限频] 跳过重复推送 fingerprint=%s", fingerprint[:16])
        notification_ledger.record(fingerprint, text, "SKIPPED_DUPLICATE",
                                   detail=f"min_interval={min_interval}")
        return False
    if notification_ledger.was_sent_recently(fingerprint, min_interval):
        logger.info("[提醒限频] 持久化台账已发送 fingerprint=%s", fingerprint[:24])
        _LAST_PUSH[fingerprint] = now
        return False
    if webhook:
        ok, detail = _send_wecom(text, webhook)
        channel = "WECOM"
    else:
        summary = "；".join(line.strip("* ") for line in text.splitlines()[1:4] if line.strip())
        ok, detail = local_notify.send(title, summary or text)
        channel = "WINDOWS_TOAST"
    notification_ledger.record(fingerprint, text, "SENT" if ok else "FAILED",
                               detail=detail, channel=channel)
    if ok:
        _LAST_PUSH[fingerprint] = now
    return ok


def notify_alerts(alerts: list, webhook: str = "", prefix: str = "📊 港股持仓预警") -> int:
    """逐条推送 alerts，带指纹去重（同一 code+level+信号 在窗口内只推一次），避免重复轰炸。
    返回实际推送条数（0 表示无预警 / 未配置 webhook / 全部已推过）。"""
    if not alerts:
        return 0
    pushed = 0
    for a in alerts:
        mark = "🔴" if a.get("level") == "danger" else "🟢"
        fp = f"alert:{a.get('code', '')}:{a.get('level', '')}:{str(a.get('msg', ''))[:24]}"
        text = f"**{prefix}**\n{mark} {a.get('name', '')}({a.get('code', '')})：{a.get('msg', '')}"
        if push_if_new(fp, text, webhook):
            pushed += 1
    return pushed


def notify_missed(top_list: list, webhook: str = "", prefix: str = "🔎 Contrarian 错杀观察") -> int:
    """错杀观察 TopN 推送（带列表指纹去重）。返回是否推送（1/0）。"""
    if not top_list:
        return 0
    fp = "missed:" + "|".join(x.get("code", "") for x in top_list[:5])
    lines = [f"**{prefix}（Top{len(top_list)}）**"]
    for i, x in enumerate(top_list, 1):
        sigs = ",".join(x.get("signals", [])) or "—"
        lines.append(f"{i}. {x.get('name', '')}({x.get('code', '')}) 评分{x.get('score', 0)} 信号:{sigs}")
    text = "\n".join(lines)
    return 1 if push_if_new(fp, text, webhook) else 0

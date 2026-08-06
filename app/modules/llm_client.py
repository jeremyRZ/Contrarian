"""轻量 LLM 决策客户端（OpenAI 兼容）。

复用 news.py 的 urllib 直连模式，无第三方依赖：
- 从 config.llm 读取 base_url / api_key / model / temperature
- 未配置 api_key 或 enabled=false 时返回 (None, "未配置")，调用方据此降级
- chat() 返回 (文本, error)；文本为 None 表示应降级

设计要点（与 news.py 一致）：
- 超时 12s，避免阻塞回测/扫描主流程
- 异常全部吞掉返回 (None, err)，不让 LLM 故障拖垮行情主链路
"""
from __future__ import annotations

import json
import urllib.request
from typing import Optional, Tuple

from ..futu_client import load_config


_CFG = None


def _cfg() -> dict:
    global _CFG
    if _CFG is None:
        _CFG = load_config()
    return _CFG


def _llm_block(cfg: Optional[dict] = None) -> dict:
    cfg = cfg or _cfg()
    return (cfg.get("llm") or {}) if isinstance(cfg, dict) else {}


def is_enabled(cfg: Optional[dict] = None) -> bool:
    blk = _llm_block(cfg)
    return bool(blk.get("enabled", True)) and bool(blk.get("api_key"))


def chat(system: str, user: str, cfg: Optional[dict] = None) -> Tuple[Optional[str], Optional[str]]:
    """调用 OpenAI 兼容的 chat/completions。返回 (content, error)。

    error 非空表示未配置或调用失败，调用方应降级处理（不阻塞主流程）。
    """
    blk = _llm_block(cfg)
    base_url = (blk.get("base_url") or "").rstrip("/")
    api_key = blk.get("api_key") or ""
    if not base_url or not api_key:
        return None, "未配置 LLM（config.llm.base_url / api_key 缺失）"
    if not blk.get("enabled", True):
        return None, "LLM 已禁用（config.llm.enabled=false）"

    model = blk.get("model") or "gpt-4o-mini"
    temperature = float(blk.get("temperature", 0.3))
    url = base_url if base_url.endswith("/chat/completions") else base_url + "/chat/completions"
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": temperature,
    }
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + api_key,
            },
        )
        with urllib.request.urlopen(req, timeout=12) as r:
            j = json.loads(r.read())
        content = j["choices"][0]["message"]["content"]
        return (content or "").strip() or None, None
    except Exception as e:  # noqa: BLE001
        return None, "LLM 调用失败：" + str(e)

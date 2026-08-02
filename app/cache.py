"""轻量内存 TTL 缓存：包裹对富途 / 东方财富等外部源的昂贵调用，降低重复扫描的实时请求量。

- 进程级字典，单进程 uvicorn 下有效（无需额外依赖）。
- 仅缓存只读返回值（调用方不得修改缓存对象）。
- 默认 TTL 300s（5 分钟），对研究型工具足够新鲜，又能显著减少 FutuOpenD 压力与扫描延迟。
"""
from __future__ import annotations

import functools
import time

_STORE: dict = {}
_DEFAULT_TTL = 300  # 秒


def cached(ttl: int = _DEFAULT_TTL, skip_first: bool = False):
    """装饰器：按「模块名.函数名 + 参数」生成 key，TTL 内返回缓存值。

    skip_first=True 时忽略第一个参数（通常是 futu client，不可哈希且不应进入 key）。
    """

    def deco(fn):
        @functools.wraps(fn)
        def wrap(*args, **kwargs):
            parts = [fn.__module__, fn.__name__]
            src = args[1:] if skip_first else args
            parts.append(":".join(str(a) for a in src))
            if kwargs:
                parts.append(":".join(f"{k}={v}" for k, v in sorted(kwargs.items())))
            key = "|".join(parts)
            now = time.time()
            hit = _STORE.get(key)
            if hit and (now - hit[1]) < ttl:
                return hit[0]
            val = fn(*args, **kwargs)
            _STORE[key] = (val, now)
            return val

        return wrap

    return deco


def clear_cache() -> None:
    """清空全部缓存（调试或强制刷新用）。"""
    _STORE.clear()

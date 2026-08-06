"""
策略参数配置（唯一真源）

- 6 大策略的开关/阈值/权重
- 八档反向信号的 8 块权重（M4 接入 reverse_signals 真正生效）
- 推送门槛（轻仓/中仓）
- LLM 决策层开关与门控阈值
- 回测验收阈值

配置落盘到 strategies.yaml（独立于 config.yaml，避免频繁改动主配置）。
未配置文件时回退到 DEFAULT_STRATEGY_CONFIG，保证行为与旧版硬编码一致。
"""
from __future__ import annotations

import copy
import os
from typing import Optional

import yaml

DEFAULT_STRATEGY_CONFIG = {
    # 6 大策略：开关 + 权重 + 阈值
    # + 新增 RSI(2) Connors 逆向低吸（价格>200日线 + RSI2<阈值 买入，趋势内抄底）
    "strategies": {
        "deep_drop":     {"enabled": True,  "weight": 3.0, "drop_pct": 25.0},
        "vol_breakout":  {"enabled": True,  "weight": 3.0, "chg_pct": 2.0, "turn": 1.5},
        "low_pe_high_div": {"enabled": True, "weight": 3.0, "pe": 10.0, "pos_pct": 30.0},
        "hstech_link":   {"enabled": True,  "weight": 2.0, "hstech_drop": -2.0},
        "panic_drop":    {"enabled": True,  "weight": 2.0, "chg_pct": -5.0, "turn": 2.0},
        "leader_pool":   {"enabled": True,  "weight": 1.0},
        # RSI2 阈值经寻优：<5 明显优于 <10（胜率 61.1% vs 59.6%，期望 +0.28% vs +0.16%）
        "rsi2_connor":   {"enabled": True,  "weight": 2.0, "rsi2_oversold": 5},
    },
    # 趋势过滤器：下跌类信号需处于上升趋势（价格>均线）才计数，
    # 避免「接飞刀」——这是把胜率从 ~45% 拉到 65%+ 的关键（Connors RSI(2) 实证）。
    "trend_filter": {
        "enabled": True,
        "period": 200,
        "apply": ["deep_drop", "vol_breakout", "low_pe_high_div", "hstech_link", "panic_drop"],
    },
    # 八档反向 8 块权重（与 reverse_signals 内部权重一一对应；M4 接入生效）
    "reverse_weights": {
        "southbound": 2.0, "buyback": 2.0, "news": 1.5, "capital_flow": 1.5,
        "valuation": 2.0, "institution": 1.5, "dividend": 1.5, "earnings": 0.5,
    },
    # 推送门槛（仓位感知）：轻仓≥light 触发，中仓/满仓≥mid
    "push": {"light": 6.0, "mid": 7.0},
    # LLM 决策层（门控阈值在此，infra 密钥在 config.yaml.llm）
    "llm": {"enabled": True, "min_win_rate": 45.0},
    # 回测引擎参数（backtrader 接管）：持有期 / 止损 / 港股成本建模
    # 参数经 /backtest/sweep 网格寻优（45只龙头池 / 250日 / 12组）后取整体期望最优组：
    # forward_days=10 · stop_pct=0.04 · rsi2_oversold=5 → 整体胜率58.1% 期望+0.19% 盈利因子1.10
    "backtest": {
        "forward_days": 10,
        "stop_pct": 0.04,      # 单笔止损 -4%（-8% 会让亏损单拖累赔率）
        "commission": 0.0005,   # 双边佣金 ≈0.05%
        "stamp": 0.001,        # 卖出印花税 0.1%（港股单边）
        "exec_on_close": True,  # 按收盘价成交(MOC)，与信号计算口径一致
    },
    # 回测验收阈值（阶段1 产出据此打可信度徽章）
    "backtest_accept": {"min_win_rate": 45.0, "min_sample": 20},
}

STRATEGIES_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "strategies.yaml"
)


def _deep_merge(base: dict, override: dict) -> dict:
    """递归合并：override 覆盖 base，保留 base 中 override 未提及的键。"""
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def load_config() -> dict:
    """读取 strategies.yaml，与默认值深合并。文件不存在/损坏则返回默认。"""
    if not os.path.exists(STRATEGIES_PATH):
        return copy.deepcopy(DEFAULT_STRATEGY_CONFIG)
    try:
        with open(STRATEGIES_PATH, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return _deep_merge(DEFAULT_STRATEGY_CONFIG, data)
    except Exception:  # noqa: BLE001
        return copy.deepcopy(DEFAULT_STRATEGY_CONFIG)


def _validate(cfg: dict) -> None:
    """基础校验，失败时抛 ValueError。确保数值类型与取值范围合理。"""
    if not isinstance(cfg, dict):
        raise ValueError("配置必须是 dict")
    s = cfg.get("strategies", {})
    if not isinstance(s, dict) or not s:
        raise ValueError("strategies 不能为空")
    for name, blk in s.items():
        if not isinstance(blk, dict):
            raise ValueError(f"策略 {name} 配置必须是 dict")
        if "enabled" in blk and not isinstance(blk["enabled"], bool):
            raise ValueError(f"策略 {name}.enabled 必须是布尔")
        for num_key in ("weight", "drop_pct", "chg_pct", "turn", "pe", "pos_pct",
                        "hstech_drop", "rsi2_oversold"):
            if num_key in blk and blk[num_key] is not None:
                try:
                    float(blk[num_key])
                except (ValueError, TypeError):
                    raise ValueError(f"策略 {name}.{num_key} 必须是数值")
    # 趋势过滤器
    tf = cfg.get("trend_filter")
    if tf is not None:
        if not isinstance(tf, dict):
            raise ValueError("trend_filter 必须是 dict")
        if "enabled" in tf and not isinstance(tf["enabled"], bool):
            raise ValueError("trend_filter.enabled 必须是布尔")
        if "period" in tf:
            try:
                int(tf["period"])
            except (ValueError, TypeError):
                raise ValueError("trend_filter.period 必须是整数")
    # 回测引擎参数
    bt = cfg.get("backtest")
    if bt is not None:
        if not isinstance(bt, dict):
            raise ValueError("backtest 必须是 dict")
        for num_key in ("forward_days", "stop_pct", "commission", "stamp"):
            if num_key in bt and bt[num_key] is not None:
                try:
                    float(bt[num_key])
                except (ValueError, TypeError):
                    raise ValueError(f"backtest.{num_key} 必须是数值")
        if "exec_on_close" in bt and not isinstance(bt["exec_on_close"], bool):
            raise ValueError("backtest.exec_on_close 必须是布尔值")
    p = cfg.get("push", {})
    for pk in ("light", "mid"):
        if pk in p:
            try:
                float(p[pk])
            except (ValueError, TypeError):
                raise ValueError(f"push.{pk} 必须是数值")


def save_config(cfg: dict) -> dict:
    """校验并落盘 strategies.yaml，返回生效后的完整配置。"""
    _validate(cfg)
    merged = _deep_merge(DEFAULT_STRATEGY_CONFIG, cfg)
    with open(STRATEGIES_PATH, "w", encoding="utf-8") as f:
        yaml.safe_dump(merged, f, allow_unicode=True, sort_keys=False)
    return merged


def reset_config() -> dict:
    """恢复默认配置并落盘。"""
    return save_config(copy.deepcopy(DEFAULT_STRATEGY_CONFIG))

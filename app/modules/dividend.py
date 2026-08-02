"""股息率 / 分红反向信号（港股高股息错杀核心维度）。

数据源：富途 get_market_snapshot 自带的 TTM 股息字段
  - dividend_ratio_ttm : TTM 股息率（%）
  - dividend_ttm       : TTM 每股股息（本币）
  - dividend_lfy       : 上一财年每股股息（用于同比判断增派 / 弃派）
无需另调 get_dividend_info（该 futu 版本无此方法），快照即有。

信号逻辑（错杀反向加分，利好红 / 利空绿）：
  yield >= 8%   → +1.5  （高股息，错杀空间大）
  yield >= 5%   → +1.0
  yield >= 3%   → +0.5
  0<yield<3%    → +0.2
  增派(ttm>lfy*1.1) → 额外 +0.3
  弃派/削减(ttm=0 但 lfy>0) → -1.0（分红断裂风险）
无分红历史 → 0
"""
from __future__ import annotations

from typing import Optional, Tuple

from . import filters


def dividend_signal(client, code: str) -> Tuple[Optional[dict], Optional[str]]:
    """计算单票股息率反向信号。返回 (dict, error)。"""
    d = filters.snapshot_dict(client, code)
    if d is None:
        return None, "行情获取失败"
    try:
        yld = float(d.get("dividend_ratio_ttm") or 0.0)
    except (ValueError, TypeError):
        yld = 0.0
    try:
        ttm = float(d.get("dividend_ttm") or 0.0)
    except (ValueError, TypeError):
        ttm = 0.0
    try:
        lfy = float(d.get("dividend_lfy") or 0.0)
    except (ValueError, TypeError):
        lfy = 0.0

    score = 0.0
    label = None
    increased = False
    omitted = False
    if yld > 0:
        if yld >= 8:
            score, label = 1.5, "高股息（≥8%）"
        elif yld >= 5:
            score, label = 1.0, "较高股息（≥5%）"
        elif yld >= 3:
            score, label = 0.5, "股息≥3%"
        else:
            score, label = 0.2, "有股息"
        # 增派 / 弃派判定
        if lfy > 0 and ttm > lfy * 1.1:
            increased = True
            score += 0.3
            label = (label or "有股息") + "·增派"
        elif lfy > 0 and ttm <= 0:
            omitted = True
            score = -1.0
            label = "弃派/削减（分红断裂风险）"
    else:
        if lfy > 0:
            # 历史有分红但 TTM 为 0：可能刚削减
            omitted = True
            score, label = -1.0, "弃派/削减（分红断裂风险）"
        else:
            label = "无分红历史"

    return {
        "yield_ratio": round(yld, 2),
        "dividend_ttm": round(ttm, 4),
        "dividend_lfy": round(lfy, 4),
        "increased": increased,
        "omitted": omitted,
        "score": round(score, 1),
        "label": label,
    }, None

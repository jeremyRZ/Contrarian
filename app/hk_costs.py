"""Single Hong Kong cash-equity transaction-cost model."""
from __future__ import annotations

import math

MODEL_ID = "hk_cash_equity_v1"
COMMISSION_RATE = 0.0003
MINIMUM_COMMISSION_HKD = 3.0
PLATFORM_FEE_HKD = 15.0
STAMP_DUTY_RATE = 0.001
STATUTORY_RATE = 0.000127
SLIPPAGE_BPS = 8.0


def order_cost(notional: float, *, include_slippage: bool = True) -> float:
    """Estimate one side; stamp duty is rounded up to the next HK dollar."""
    value = abs(float(notional))
    if value <= 0:
        return 0.0
    fees = (max(MINIMUM_COMMISSION_HKD, value * COMMISSION_RATE)
            + PLATFORM_FEE_HKD + math.ceil(value * STAMP_DUTY_RATE)
            + value * STATUTORY_RATE)
    if include_slippage:
        fees += value * SLIPPAGE_BPS / 10_000
    return fees


def affordable_board_lot(price: float, budget: float, lot_size: int) -> int:
    """Largest board-lot buy whose notional plus all estimated costs fits budget."""
    if price <= 0 or budget <= 0 or lot_size <= 0:
        return 0
    qty = int(budget // (price * lot_size)) * lot_size
    while qty and price * qty + order_cost(price * qty) > budget:
        qty -= lot_size
    return max(qty, 0)

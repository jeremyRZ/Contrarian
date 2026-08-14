from __future__ import annotations

from dataclasses import dataclass
from datetime import time


@dataclass(frozen=True)
class MarketRules:
    market: str
    currency: str
    benchmark: str
    lot_size: int
    settlement: str
    timezone: str
    sessions: tuple[tuple[time, time], ...]
    default_price_limit: float | None
    buy_commission: float
    sell_commission: float
    minimum_commission: float
    sell_stamp_tax: float = 0.0

    def round_buy_quantity(self, quantity: float) -> int:
        return max(0, int(quantity) // self.lot_size * self.lot_size)

    def commission(self, side: str, quantity: int, price: float) -> float:
        notional = abs(int(quantity)) * float(price)
        rate = self.sell_commission if side.upper() == "SELL" else self.buy_commission
        fee = max(self.minimum_commission, notional * rate)
        if side.upper() == "SELL":
            fee += notional * self.sell_stamp_tax
        return fee


_RULES = {
    "HK": MarketRules("HK", "HKD", "HK.800000", 1, "T+2", "Asia/Hong_Kong",
                      ((time(9, 30), time(12)), (time(13), time(16))), None,
                      .0005, .0005, 0.0, .001),
    "CN": MarketRules("CN", "CNY", "SH.000300", 100, "T+1", "Asia/Shanghai",
                      ((time(9, 30), time(11, 30)), (time(13), time(15))), .10,
                      .0003, .0003, 5.0, .0005),
    "US": MarketRules("US", "USD", "US.SPY", 1, "T+1", "America/New_York",
                      ((time(9, 30), time(16)),), None, 0.0, 0.0, 0.0),
}


def get_market_rules(market_or_code: str) -> MarketRules:
    key = str(market_or_code).upper().split(".", 1)[0]
    if key in {"SH", "SZ", "BJ"}: key = "CN"
    if key not in _RULES:
        raise ValueError(f"不支持的市场: {market_or_code}")
    return _RULES[key]


def cn_price_limit(code: str, *, is_st: bool = False) -> float:
    """Current regular A-share daily price-limit bands for execution modelling."""
    value = str(code).upper()
    if value.startswith("BJ."): return .30
    if value.startswith("SH.688") or value.startswith("SZ.300"): return .20
    if is_st: return .05
    return .10


def cn_lot_size(code: str) -> int:
    """Minimum regular buy declaration; STAR Market currently starts at 200."""
    return 200 if str(code).upper().startswith("SH.688") else 100

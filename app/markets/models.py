from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class ValidationStatus(str, Enum):
    RESEARCH_ONLY = "RESEARCH_ONLY"
    PAPER = "PAPER"
    ACTIVE = "ACTIVE"
    REJECTED = "REJECTED"


@dataclass(frozen=True)
class Security:
    code: str
    symbol: str
    market: str
    exchange: str
    currency: str
    asset_type: str = "STOCK"
    name: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Signal:
    strategy_id: str
    code: str
    market: str
    action: str
    validation_status: ValidationStatus = ValidationStatus.RESEARCH_ONLY
    reason: str = ""
    as_of: str | None = None
    signal_price: float | None = None
    entry_trigger: float | None = None
    invalid_price: float | None = None
    suggested_qty: int = 0
    execute_at: str = "NEXT_SESSION_OPEN"
    evidence: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["validation_status"] = self.validation_status.value
        return value

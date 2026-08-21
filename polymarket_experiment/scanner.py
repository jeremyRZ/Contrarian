"""Paper scanner for buying both sides of a binary Polymarket market.

The module never signs or submits orders.  It only reads public Gamma/CLOB
endpoints and estimates whether equal-sized YES and NO purchases could return
more than their executable cost at settlement.
"""

from __future__ import annotations

import argparse
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlencode
from urllib.request import Request, urlopen


GAMMA_URL = "https://gamma-api.polymarket.com"
CLOB_URL = "https://clob.polymarket.com"
ONE = Decimal("1")


def _decimal(value: Any, default: str = "0") -> Decimal:
    try:
        return Decimal(str(value))
    except (ValueError, TypeError):
        return Decimal(default)


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        parsed = json.loads(value)
        if isinstance(parsed, list):
            return parsed
    return []


def _get_json(base: str, path: str, params: dict[str, Any]) -> Any:
    url = f"{base}{path}?{urlencode(params)}"
    request = Request(url, headers={"User-Agent": "contrarian-paper-scanner/0.1"})
    with urlopen(request, timeout=20) as response:  # nosec: public fixed hosts
        return json.load(response)


def taker_fee(shares: Decimal, price: Decimal, rate: Decimal) -> Decimal:
    """Current documented Polymarket fee formula for a taker fill."""
    if rate <= 0:
        return Decimal("0")
    return shares * rate * price * (ONE - price)


@dataclass(frozen=True)
class FillEstimate:
    shares: Decimal
    notional: Decimal
    average_price: Decimal
    worst_price: Decimal


def walk_asks(levels: Iterable[dict[str, Any]], shares: Decimal) -> FillEstimate | None:
    """Walk asks by ascending price and estimate an all-or-none paper fill."""
    remaining = shares
    notional = Decimal("0")
    worst = Decimal("0")
    ordered = sorted(levels, key=lambda level: _decimal(level.get("price")))
    for level in ordered:
        price = _decimal(level.get("price"))
        available = _decimal(level.get("size"))
        if price <= 0 or available <= 0:
            continue
        quantity = min(remaining, available)
        notional += quantity * price
        remaining -= quantity
        worst = price
        if remaining <= 0:
            return FillEstimate(shares, notional, notional / shares, worst)
    return None


@dataclass(frozen=True)
class Observation:
    observed_at: str
    market_id: str
    condition_id: str
    question: str
    slug: str
    shares: str
    yes_average_ask: str
    no_average_ask: str
    gross_cost: str
    estimated_fees: str
    latency_slippage_buffer: str
    net_edge: str
    net_roi: str
    fee_rate: str
    fees_enabled: bool
    yes_book_timestamp: str
    no_book_timestamp: str
    snapshot_skew_ms: int | None


def _timestamp_ms(value: Any) -> int | None:
    try:
        numeric = int(value)
    except (TypeError, ValueError):
        return None
    return numeric if numeric > 10_000_000_000 else numeric * 1000


def evaluate_market(
    market: dict[str, Any],
    yes_book: dict[str, Any],
    no_book: dict[str, Any],
    shares: Decimal,
    slippage_bps_per_leg: Decimal,
) -> Observation | None:
    outcomes = [str(item).lower() for item in _json_list(market.get("outcomes"))]
    tokens = [str(item) for item in _json_list(market.get("clobTokenIds"))]
    if outcomes != ["yes", "no"] or len(tokens) != 2:
        return None

    yes_fill = walk_asks(yes_book.get("asks", []), shares)
    no_fill = walk_asks(no_book.get("asks", []), shares)
    if yes_fill is None or no_fill is None:
        return None

    schedule = market.get("feeSchedule") or {}
    fees_enabled = bool(market.get("feesEnabled"))
    fee_rate = _decimal(schedule.get("rate")) if fees_enabled else Decimal("0")
    fees = taker_fee(shares, yes_fill.average_price, fee_rate)
    fees += taker_fee(shares, no_fill.average_price, fee_rate)
    gross_cost = yes_fill.notional + no_fill.notional
    slippage_buffer = gross_cost * slippage_bps_per_leg / Decimal("10000")
    payout = shares
    net_edge = payout - gross_cost - fees - slippage_buffer
    total_cost = gross_cost + fees + slippage_buffer

    yes_ms = _timestamp_ms(yes_book.get("timestamp"))
    no_ms = _timestamp_ms(no_book.get("timestamp"))
    skew = abs(yes_ms - no_ms) if yes_ms is not None and no_ms is not None else None
    return Observation(
        observed_at=datetime.now(timezone.utc).isoformat(),
        market_id=str(market.get("id", "")),
        condition_id=str(market.get("conditionId", "")),
        question=str(market.get("question", "")),
        slug=str(market.get("slug", "")),
        shares=str(shares),
        yes_average_ask=str(yes_fill.average_price),
        no_average_ask=str(no_fill.average_price),
        gross_cost=str(gross_cost),
        estimated_fees=str(fees),
        latency_slippage_buffer=str(slippage_buffer),
        net_edge=str(net_edge),
        net_roi=str(net_edge / total_cost if total_cost > 0 else Decimal("0")),
        fee_rate=str(fee_rate),
        fees_enabled=fees_enabled,
        yes_book_timestamp=str(yes_book.get("timestamp", "")),
        no_book_timestamp=str(no_book.get("timestamp", "")),
        snapshot_skew_ms=skew,
    )


def discover_markets(limit: int) -> list[dict[str, Any]]:
    result = _get_json(
        GAMMA_URL,
        "/markets",
        {
            "active": "true",
            "closed": "false",
            "limit": limit,
            "order": "volume24hr",
            "ascending": "false",
        },
    )
    return [market for market in result if market.get("acceptingOrders")]


def fetch_book(token_id: str) -> dict[str, Any]:
    return _get_json(CLOB_URL, "/book", {"token_id": token_id})


def scan_once(limit: int, shares: Decimal, slippage_bps: Decimal) -> list[Observation]:
    candidates: list[dict[str, Any]] = []
    for market in discover_markets(limit):
        outcomes = [str(item).lower() for item in _json_list(market.get("outcomes"))]
        tokens = [str(item) for item in _json_list(market.get("clobTokenIds"))]
        if outcomes == ["yes", "no"] and len(tokens) == 2:
            candidates.append(market)

    def evaluate_candidate(market: dict[str, Any]) -> Observation | None:
        tokens = [str(item) for item in _json_list(market.get("clobTokenIds"))]
        try:
            with ThreadPoolExecutor(max_workers=2) as leg_pool:
                yes_future = leg_pool.submit(fetch_book, tokens[0])
                no_future = leg_pool.submit(fetch_book, tokens[1])
                return evaluate_market(
                    market,
                    yes_future.result(),
                    no_future.result(),
                    shares,
                    slippage_bps,
                )
        except Exception as exc:  # keep a broad market scan alive on isolated API failures
            print(f"skip market {market.get('id')}: {exc}")
            return None

    observations: list[Observation] = []
    with ThreadPoolExecutor(max_workers=12) as pool:
        futures = [pool.submit(evaluate_candidate, market) for market in candidates]
        for future in as_completed(futures):
            observation = future.result()
            if observation is not None:
                observations.append(observation)
    return observations


def append_jsonl(path: Path, observations: Iterable[Observation]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for observation in observations:
            handle.write(json.dumps(asdict(observation), ensure_ascii=False) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only Polymarket binary arbitrage scanner")
    parser.add_argument("--limit", type=int, default=100, help="markets to inspect")
    parser.add_argument("--shares", type=Decimal, default=Decimal("10"))
    parser.add_argument("--slippage-bps", type=Decimal, default=Decimal("10"))
    parser.add_argument("--output", type=Path, default=Path("data/polymarket_observations.jsonl"))
    args = parser.parse_args()

    observations = scan_once(args.limit, args.shares, args.slippage_bps)
    append_jsonl(args.output, observations)
    positive = [item for item in observations if Decimal(item.net_edge) > 0]
    summary = {
        "scanned_with_books": len(observations),
        "positive_after_cost_buffer": len(positive),
        "best": asdict(max(observations, key=lambda item: Decimal(item.net_edge))) if observations else None,
        "output": str(args.output),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

from decimal import Decimal

from polymarket_experiment.scanner import evaluate_market, taker_fee, walk_asks


def test_walk_asks_uses_depth_in_price_order():
    fill = walk_asks(
        [
            {"price": "0.52", "size": "8"},
            {"price": "0.50", "size": "4"},
        ],
        Decimal("10"),
    )
    assert fill is not None
    assert fill.notional == Decimal("5.12")
    assert fill.average_price == Decimal("0.512")
    assert fill.worst_price == Decimal("0.52")


def test_walk_asks_rejects_insufficient_depth():
    assert walk_asks([{"price": "0.5", "size": "2"}], Decimal("3")) is None


def test_taker_fee_matches_documented_formula():
    assert taker_fee(Decimal("10"), Decimal("0.4"), Decimal("0.05")) == Decimal("0.1200")


def test_evaluate_market_deducts_two_leg_fees_and_buffer():
    market = {
        "id": "1",
        "conditionId": "condition",
        "question": "Example?",
        "slug": "example",
        "outcomes": '["Yes", "No"]',
        "clobTokenIds": '["yes-token", "no-token"]',
        "feesEnabled": True,
        "feeSchedule": {"rate": 0.05},
    }
    yes_book = {"asks": [{"price": "0.40", "size": "10"}], "timestamp": "1000"}
    no_book = {"asks": [{"price": "0.55", "size": "10"}], "timestamp": "1001"}

    result = evaluate_market(market, yes_book, no_book, Decimal("10"), Decimal("10"))

    assert result is not None
    assert Decimal(result.gross_cost) == Decimal("9.50")
    assert Decimal(result.estimated_fees) == Decimal("0.24375")
    assert Decimal(result.latency_slippage_buffer) == Decimal("0.00950")
    assert Decimal(result.net_edge) == Decimal("0.24675")
    assert result.snapshot_skew_ms == 1000


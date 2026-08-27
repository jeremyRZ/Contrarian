from app.hk_costs import affordable_board_lot, order_cost


def test_affordability_includes_minimum_fees_slippage_and_board_lot():
    assert order_cost(5500) > 18
    assert affordable_board_lot(27.5, 5520, 200) == 0
    assert affordable_board_lot(27.5, 5600, 200) == 200

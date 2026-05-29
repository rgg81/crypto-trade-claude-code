import pytest

from futures_fund.orders import build_orders, round_price, round_qty


def test_round_price_to_tick():
    assert round_price(73654.317, 0.1) == pytest.approx(73654.3)
    assert round_price(100.0, 0.0) == 100.0  # no tick -> unchanged


def test_round_qty_floors_to_step():
    assert round_qty(0.123987, 0.001) == pytest.approx(0.123)
    assert round_qty(0.0009, 0.001) == pytest.approx(0.0)  # below one step -> 0


def test_build_orders_long_has_entry_and_reduceonly_protection():
    orders = build_orders("BTCUSDT", "long", qty=0.1234, entry=100.0, stop=95.0,
                          take_profits=[115.0], tick=0.1, step=0.001)
    assert len(orders) == 3
    entry, stop, tp = orders
    assert (entry["type"] == "market" and entry["side"] == "buy"
            and entry["amount"] == pytest.approx(0.123))
    assert stop["type"] == "STOP_MARKET" and stop["side"] == "sell"
    assert (stop["params"]["reduceOnly"] is True
            and stop["params"]["stopPrice"] == pytest.approx(95.0))
    assert (tp["type"] == "TAKE_PROFIT_MARKET" and tp["side"] == "sell"
            and tp["params"]["reduceOnly"] is True)


def test_build_orders_short_sides_flip():
    orders = build_orders("BTCUSDT", "short", qty=0.1, entry=100.0, stop=105.0,
                          take_profits=[85.0], tick=0.1, step=0.001)
    assert orders[0]["side"] == "sell"   # entry
    assert orders[1]["side"] == "buy" and orders[1]["type"] == "STOP_MARKET"  # close


def test_build_orders_empty_when_qty_rounds_to_zero():
    assert build_orders("BTCUSDT", "long", qty=0.0004, entry=100.0, stop=95.0,
                        take_profits=[115.0], tick=0.1, step=0.001) == []


def test_build_orders_no_tp_omits_tp_order():
    orders = build_orders("BTCUSDT", "long", qty=0.1, entry=100.0, stop=95.0,
                          take_profits=[], tick=0.1, step=0.001)
    assert len(orders) == 2 and orders[1]["type"] == "STOP_MARKET"

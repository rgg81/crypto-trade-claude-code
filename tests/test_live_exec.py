import pytest

from futures_fund.live_exec import LiveExecutor


class FakeCcxt:
    def __init__(self):
        self.calls = []

    def set_margin_mode(self, mode, symbol):
        self.calls.append(("margin", mode, symbol))

    def set_leverage(self, lev, symbol):
        self.calls.append(("leverage", lev, symbol))

    def create_order(self, symbol, type_, side, amount, price=None, params=None):
        self.calls.append(("order", symbol, type_, side, amount, params or {}))
        return {"id": f"{type_}-{side}", "status": "open"}

    def cancel_all_orders(self, symbol):
        self.calls.append(("cancel_all", symbol))
        return []


def test_place_book_refuses_without_confirm_live():
    ex = LiveExecutor(FakeCcxt())
    with pytest.raises(RuntimeError):
        ex.place_book([{"symbol": "BTCUSDT", "type": "market", "side": "buy", "amount": 0.1}],
                      confirm_live=False)


def test_prepare_sets_margin_and_leverage():
    fake = FakeCcxt()
    LiveExecutor(fake).prepare("BTCUSDT", leverage=5.0, margin_mode="isolated")
    assert ("margin", "isolated", "BTCUSDT") in fake.calls
    assert ("leverage", 5, "BTCUSDT") in fake.calls


def test_place_book_creates_each_order_with_confirm():
    fake = FakeCcxt()
    orders = [
        {"symbol": "BTCUSDT", "type": "market", "side": "buy", "amount": 0.1},
        {"symbol": "BTCUSDT", "type": "STOP_MARKET", "side": "sell", "amount": 0.1,
         "params": {"stopPrice": 95.0, "reduceOnly": True}},
    ]
    results = LiveExecutor(fake).place_book(orders, confirm_live=True)
    assert len(results) == 2
    order_calls = [c for c in fake.calls if c[0] == "order"]
    assert len(order_calls) == 2
    assert order_calls[1][5]["reduceOnly"] is True  # stop carries reduceOnly


def test_prepare_tolerates_margin_mode_already_set():
    class Boom(FakeCcxt):
        def set_margin_mode(self, mode, symbol):
            raise Exception("No need to change margin type.")
    fake = Boom()
    LiveExecutor(fake).prepare("BTCUSDT", leverage=3.0)  # must not raise
    assert ("leverage", 3, "BTCUSDT") in fake.calls

from __future__ import annotations


class LiveExecutor:
    """Places REAL orders via a ccxt client. Refuses to place anything without an explicit
    confirm_live=True (safety invariant 1). Stops/TPs must already be reduceOnly (see orders.py).
    Inject a fake client in tests; never reached in paper mode."""

    def __init__(self, client):
        self.client = client

    def prepare(self, symbol: str, leverage: float, margin_mode: str = "isolated") -> None:
        try:
            self.client.set_margin_mode(margin_mode, symbol)
        except Exception:
            pass  # Binance raises if the margin mode is already set — benign
        self.client.set_leverage(int(leverage), symbol)

    def place_book(self, orders: list[dict], *, confirm_live: bool) -> list:
        if not confirm_live:
            raise RuntimeError("LiveExecutor.place_book refused: confirm_live is not True")
        results = []
        for o in orders:
            results.append(self.client.create_order(
                o["symbol"], o["type"], o["side"], o["amount"], o.get("price"), o.get("params", {})
            ))
        return results

    def cancel_all(self, symbol: str):
        return self.client.cancel_all_orders(symbol)

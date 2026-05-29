from __future__ import annotations

import math

from futures_fund.models import Direction


def round_price(price: float, tick: float) -> float:
    if tick <= 0:
        return price
    return round(round(price / tick) * tick, 10)


def round_qty(qty: float, step: float) -> float:
    if step <= 0:
        return qty
    return round(math.floor(qty / step) * step, 10)


def _open_side(direction: Direction) -> str:
    return "buy" if direction == "long" else "sell"


def _close_side(direction: Direction) -> str:
    return "sell" if direction == "long" else "buy"


def build_orders(symbol: str, direction: Direction, qty: float, entry: float, stop: float,
                 take_profits: list[float], tick: float, step: float) -> list[dict]:
    """Build the exchange order set for one position: a market entry plus reduceOnly STOP_MARKET
    and TAKE_PROFIT_MARKET protection, with price->tick and qty->step rounding. Empty if the
    rounded quantity is zero."""
    q = round_qty(qty, step)
    if q <= 0:
        return []
    cs = _close_side(direction)
    orders = [{"symbol": symbol, "type": "market", "side": _open_side(direction), "amount": q}]
    orders.append({"symbol": symbol, "type": "STOP_MARKET", "side": cs, "amount": q,
                   "params": {"stopPrice": round_price(stop, tick), "reduceOnly": True}})
    if take_profits:
        orders.append({"symbol": symbol, "type": "TAKE_PROFIT_MARKET", "side": cs, "amount": q,
                       "params": {"stopPrice": round_price(take_profits[0], tick),
                                  "reduceOnly": True}})
    return orders

"""Partial-reduce ("trim") helper — bank a fraction of a winning position and keep a smaller
runner. Pure: no I/O, no balance mutation. The banked slice reuses the PROTECTED close_at_mark
read-only on a temp slice Position; the caller (gate_execute_step) credits the wallet and carries
the runner. v1: market-neutral (qty-based; PnL sign handled inside close_at_mark), discretionary."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from futures_fund.executor import close_at_mark
from futures_fund.exits import ClosedTrade
from futures_fund.models import SymbolSpec
from futures_fund.orders import round_qty
from futures_fund.state import Position


@dataclass
class ReduceResult:
    kind: Literal["reduced", "promote_full", "noop_dust"]
    closed_trade: ClosedTrade | None = None
    runner: Position | None = None


def reduce_position(position: Position, mark: float, fraction: float, *,
                    funding_rate: float, funding_events: int, slippage_bps: float,
                    spec: SymbolSpec, pay_bnb: bool = False) -> ReduceResult:
    """Split `position` into a banked slice (a fraction of qty, closed at mark) and a runner.

    - slice qty is floored to the lot step; if it rounds to 0 -> noop_dust (position left whole).
    - if the runner would fall below min_notional -> promote_full (caller force-closes 100%).
    - otherwise bank the slice via close_at_mark and return the reduced runner. entry/leverage/
      liq_price/decision_id are unchanged; margin scales proportionally. The old liq_price is KEPT:
      a proportional qty+margin cut leaves liq geometry unchanged, and the larger-notional liq is
      conservative (never closer than reality) — so no protected liquidation recompute is needed.
    """
    slice_qty = round_qty(fraction * position.qty, spec.step_size)
    if slice_qty <= 0:
        return ReduceResult(kind="noop_dust")
    remaining = position.qty - slice_qty
    if remaining * mark < spec.min_notional:
        return ReduceResult(kind="promote_full")
    slice_pos = position.model_copy(update={"qty": slice_qty})
    ct = close_at_mark(slice_pos, mark, funding_rate=funding_rate, funding_events=funding_events,
                       slippage_bps=slippage_bps, pay_bnb=pay_bnb)
    runner = position.model_copy(update={
        "qty": remaining, "margin": position.margin * (remaining / position.qty)})
    return ReduceResult(kind="reduced", closed_trade=ct, runner=runner)

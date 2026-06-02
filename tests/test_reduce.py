"""Unit tests for the partial-reduce helper (market-neutral trim). The banked slice must equal a
FULL close of that fraction (reusing the protected close math), runner qty/margin shrink, and the
dust guards (promote-to-full / noop) fire symmetrically for long and short."""
from datetime import UTC, datetime

from futures_fund.executor import close_at_mark
from futures_fund.models import MmrBracket, SymbolSpec
from futures_fund.reduce import ReduceResult, reduce_position
from futures_fund.state import Position

_TS = datetime(2026, 2, 1, tzinfo=UTC)


def _spec(step=0.001, min_notional=5.0):
    return SymbolSpec(symbol="ETHUSDT", tick_size=0.01, step_size=step, min_notional=min_notional,
                      mmr_brackets=[MmrBracket(notional_floor=0, notional_cap=1_000_000,
                                               mmr=0.004, maint_amount=0.0, max_leverage=125)])


def _pos(direction="long", qty=1.0, entry=100.0, stop=90.0):
    return Position(symbol="ETHUSDT", direction=direction, qty=qty, entry=entry, stop=stop,
                    take_profits=[130.0], leverage=3.0, margin=33.3, liq_price=70.0,
                    opened_cycle=1, opened_ts=_TS, decision_id="d1")


def test_reduce_banks_slice_and_keeps_runner():
    pos = _pos(qty=1.0, entry=100.0)
    res = reduce_position(pos, mark=120.0, fraction=0.5, funding_rate=0.0001,
                          funding_events=1, slippage_bps=2.0, spec=_spec())
    assert isinstance(res, ReduceResult) and res.kind == "reduced"
    # runner: half the qty + half the margin; entry/leverage/liq/decision_id unchanged
    assert res.runner.qty == 0.5 and res.runner.entry == 100.0
    assert res.runner.margin == 33.3 * 0.5 and res.runner.leverage == 3.0
    assert res.runner.liq_price == 70.0 and res.runner.decision_id == "d1"
    # banked PnL is EXACTLY a full close of the 0.5 slice (reuses protected close math)
    expected = close_at_mark(pos.model_copy(update={"qty": 0.5}), 120.0,
                             funding_rate=0.0001, funding_events=1, slippage_bps=2.0)
    assert res.closed_trade.realized_pnl == expected.realized_pnl
    assert res.closed_trade.qty == 0.5 and res.closed_trade.realized_pnl > 0


def test_reduce_is_symmetric_for_short():
    # winning short (entry 100, mark 80) banks positive PnL on the slice
    pos = _pos(direction="short", qty=1.0, entry=100.0, stop=110.0)
    res = reduce_position(pos, mark=80.0, fraction=0.5, funding_rate=0.0001,
                          funding_events=1, slippage_bps=2.0, spec=_spec())
    assert res.kind == "reduced" and res.runner.qty == 0.5 and res.runner.direction == "short"
    expected = close_at_mark(pos.model_copy(update={"qty": 0.5}), 80.0,
                             funding_rate=0.0001, funding_events=1, slippage_bps=2.0)
    assert res.closed_trade.realized_pnl == expected.realized_pnl
    assert res.closed_trade.realized_pnl > 0


def test_reduce_credits_received_funding_on_slice():
    # short with positive funding RECEIVES carry; banked funding must stay signed (not clamped)
    pos = _pos(direction="short", qty=1.0, entry=100.0, stop=110.0)
    res = reduce_position(pos, mark=95.0, fraction=0.5, funding_rate=0.0005,
                          funding_events=3, slippage_bps=2.0, spec=_spec())
    expected = close_at_mark(pos.model_copy(update={"qty": 0.5}), 95.0,
                             funding_rate=0.0005, funding_events=3, slippage_bps=2.0)
    assert res.closed_trade.funding == expected.funding  # identical signed funding on the slice


def test_reduce_promotes_to_full_close_when_runner_below_min_notional():
    pos = _pos(qty=1.0, entry=100.0)
    # fraction 0.99 -> remaining 0.01 ; 0.01 * 120 = 1.2 < min_notional 5.0 -> promote
    res = reduce_position(pos, mark=120.0, fraction=0.99, funding_rate=0.0001,
                          funding_events=1, slippage_bps=2.0, spec=_spec(min_notional=5.0))
    assert res.kind == "promote_full"
    assert res.closed_trade is None and res.runner is None


def test_reduce_noop_when_slice_rounds_below_step():
    pos = _pos(qty=1.0, entry=100.0)
    # fraction 0.0005 * qty 1.0 = 0.0005 ; floor to step 0.001 -> 0 -> noop
    res = reduce_position(pos, mark=120.0, fraction=0.0005, funding_rate=0.0001,
                          funding_events=1, slippage_bps=2.0, spec=_spec(step=0.001))
    assert res.kind == "noop_dust"
    assert res.closed_trade is None and res.runner is None

# Partial-Reduce / Trim (v1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the Trader bank a fraction of a winning position at mark and carry a smaller runner, via a new `reduce` holdings-review action — with zero edits to protected modules.

**Architecture:** A new pure helper `futures_fund/reduce.py` splits a `Position` into a banked slice (closed by calling the protected `executor.close_at_mark` read-only on a temp fractional Position) and a reduced runner. A new `action == "reduce"` branch in the non-protected `gate_execute_step` holdings-review loop (`futures_fund/orchestration.py`) calls the helper, credits `account.balance` in memory, and carries the runner; `execute_proposals` then reserves heat on the post-reduce qty and persists. A runner that would fall below `min_notional` is promoted to a full close via the existing `force_close` set.

**Tech Stack:** Python 3.11, pydantic models, pytest, `uv run pytest`. Reuses `executor.close_at_mark`, `orders.round_qty`, `costs.count_funding_events`.

**Spec:** `docs/superpowers/specs/2026-06-02-partial-reduce-design.md`

**Baseline:** `uv run pytest` is green at 486 passed before starting.

**Protected modules (NEVER edit):** `risk_gate`, `executor`, `exits`, `consolidation`, `policy`, `liquidation`, `sizing`, `cycle`. This plan touches none of them — it only *imports* `close_at_mark` (executor) and `_SLIPPAGE_BPS`/`fetch_context` (cycle) read-only.

---

## File Structure

| File | Responsibility | New/Modify |
|---|---|---|
| `futures_fund/reduce.py` | Pure split helper: `ReduceResult` + `reduce_position()`. No I/O, no balance mutation. | **New** |
| `tests/test_reduce.py` | Unit tests for the helper (banked-PnL == slice close, symmetry, dust/promote). | **New** |
| `futures_fund/orchestration.py` | `_valid_reduce_fraction()` + `reduce` branch in `gate_execute_step` loop + report keys. | Modify (~lines 8, 384–404, 489–491) |
| `tests/test_orchestration.py` | Integration: reduce halves qty/banks PnL/keeps runner; short symmetry; bad-fraction drop; dust→full-close. | Modify (add tests) |
| `tests/test_gate_wiring.py` | Telemetry keys + reduce honored on HALT. | Modify (add tests) |
| `tests/test_exposure.py` | A reduce lowers book gross/tilt. | Modify (add test) |
| `agents/trader.md`, `SKILL.md` | Document the `reduce` action for the Trader. | Modify |

---

## Task 1: `reduce.py` helper (pure split + banked-slice close)

**Files:**
- Create: `futures_fund/reduce.py`
- Create: `tests/test_reduce.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_reduce.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_reduce.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'futures_fund.reduce'`.

- [ ] **Step 3: Write the helper**

Create `futures_fund/reduce.py`:

```python
"""Partial-reduce ("trim") helper — bank a fraction of a winning position and keep a smaller
runner. Pure: no I/O, no balance mutation. The banked slice reuses the PROTECTED close_at_mark
read-only on a temp slice Position; the caller (gate_execute_step) credits the wallet and carries
the runner. v1: market-neutral (qty-based; PnL sign handled inside close_at_mark), discretionary."""
from __future__ import annotations

from dataclasses import dataclass

from futures_fund.executor import close_at_mark
from futures_fund.exits import ClosedTrade
from futures_fund.models import SymbolSpec
from futures_fund.orders import round_qty
from futures_fund.state import Position


@dataclass
class ReduceResult:
    kind: str  # "reduced" | "promote_full" | "noop_dust"
    closed_trade: ClosedTrade | None = None
    runner: Position | None = None


def reduce_position(position: Position, mark: float, fraction: float, *,
                    funding_rate: float, funding_events: int, slippage_bps: float,
                    spec: SymbolSpec, pay_bnb: bool = False) -> ReduceResult:
    """Split `position` into a banked slice (a fraction of qty, closed at mark) and a runner.

    - slice qty is floored to the lot step; if it rounds to 0 -> noop_dust (leave the position whole).
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_reduce.py -q`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add futures_fund/reduce.py tests/test_reduce.py
git commit -m "feat(reduce): pure partial-reduce helper reusing close_at_mark on a slice"
```

---

## Task 2: Wire the `reduce` action into `gate_execute_step`

**Files:**
- Modify: `futures_fund/orchestration.py` (import at line 8; loop at 384–404; report at 489–491)
- Test: `tests/test_orchestration.py`

Context — the holdings-review loop builds `new_positions` and a local `trailed` counter, then sets `report["trailed"] = trailed` AFTER `execute_proposals` returns (because `report` is created by `execute_proposals`). The reduce branch follows the same pattern: accumulate into locals, credit `account.balance` in the loop, attach report keys afterward. `execute_proposals` receives the same in-memory `account`/`positions` and persists them.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_orchestration.py` (after `test_holdings_review_rejects_trail_past_mark`):

```python
def test_holdings_review_reduce_banks_half_and_keeps_runner(tmp_path):
    from futures_fund.state import load_account
    state_dir, memory_dir, ex = _seed_holding(tmp_path)  # ETH long entry 100 qty 1.0, mark ~147
    default = _settings().account_size_usdt
    report = gate_execute_step(
        ex, _settings(), state_dir, memory_dir, now=dt.datetime(2026, 3, 1, tzinfo=UTC),
        cycle_no=2, proposals=[],
        management=[{"symbol": "ETHUSDT", "action": "reduce", "reduce_fraction": 0.5}])
    assert report["reduced"] == 1 and report["banked_pnl"] > 0 and report["closed"] == 0
    pos = load_positions(state_dir)
    assert len(pos) == 1 and pos[0].symbol == "ETHUSDT" and pos[0].qty == 0.5  # runner kept
    # only the reduce moved the wallet (no opens/closes) -> balance == default + banked
    assert load_account(state_dir, default).balance == default + report["banked_pnl"]


def test_holdings_review_reduce_works_for_short(tmp_path):
    from futures_fund.state import Position, save_positions
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    held = Position(symbol="ETHUSDT", direction="short", qty=2.0, entry=200.0, stop=210.0,
                    take_profits=[100.0], leverage=3.0, margin=133.0, liq_price=300.0,
                    opened_cycle=1, opened_ts=dt.datetime(2026, 2, 1, tzinfo=UTC))
    save_positions(state_dir, [held])
    ex = _UnionExchange({"BTC/USDT:USDT": _uptrend(), "ETH/USDT:USDT": _uptrend()},
                        {"ETHUSDT": "ETH/USDT:USDT"})  # ETH mark ~147 < entry 200 -> winning short
    report = gate_execute_step(
        ex, _settings(), state_dir, memory_dir, now=dt.datetime(2026, 3, 1, tzinfo=UTC),
        cycle_no=2, proposals=[],
        management=[{"symbol": "ETHUSDT", "action": "reduce", "reduce_fraction": 0.5}])
    assert report["reduced"] == 1 and report["banked_pnl"] > 0
    assert load_positions(state_dir)[0].qty == 1.0  # half of 2.0


def test_holdings_review_reduce_drops_bad_fraction(tmp_path):
    state_dir, memory_dir, ex = _seed_holding(tmp_path)
    report = gate_execute_step(
        ex, _settings(), state_dir, memory_dir, now=dt.datetime(2026, 3, 1, tzinfo=UTC),
        cycle_no=2, proposals=[],
        management=[{"symbol": "ETHUSDT", "action": "reduce", "reduce_fraction": 1.5}])
    assert report["reduced"] == 0 and report["reduce_dropped"] == 1
    assert load_positions(state_dir)[0].qty == 1.0  # untouched


def test_holdings_review_reduce_promotes_dust_to_full_close(tmp_path):
    state_dir, memory_dir, ex = _seed_holding(tmp_path)  # qty 1.0, mark ~147, min_notional 5.0
    report = gate_execute_step(  # remaining 0.01 * 147 = ~1.47 < 5 -> full close
        ex, _settings(), state_dir, memory_dir, now=dt.datetime(2026, 3, 1, tzinfo=UTC),
        cycle_no=2, proposals=[],
        management=[{"symbol": "ETHUSDT", "action": "reduce", "reduce_fraction": 0.99}])
    assert report["closed"] == 1 and report["reduced"] == 0
    assert load_positions(state_dir) == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_orchestration.py -k reduce -q`
Expected: FAIL — `KeyError: 'reduced'` (the report key does not exist yet; the directive is ignored).

- [ ] **Step 3: Add the import**

In `futures_fund/orchestration.py`, line 8 currently reads:

```python
from futures_fund.cycle import audit_and_reflect, execute_proposals, fetch_context
```

Replace it with:

```python
from futures_fund.cycle import (
    _SLIPPAGE_BPS,
    audit_and_reflect,
    execute_proposals,
    fetch_context,
)
from futures_fund.costs import count_funding_events
from futures_fund.reduce import reduce_position
```

- [ ] **Step 4: Add the `_valid_reduce_fraction` helper**

In `futures_fund/orchestration.py`, add this module-level function just above `gate_execute_step` (its `def` is near line 339):

```python
def _valid_reduce_fraction(v) -> float | None:
    """Coerce a reduce_fraction directive value to a float in (0, 1); None if invalid."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if 0.0 < f < 1.0 else None
```

- [ ] **Step 5: Add the reduce branch + local accumulators**

In `gate_execute_step`, the loop currently is (lines 384–404):

```python
    force_close, trailed = set(), 0
    new_positions = []
    for p in positions:
        m = by_raw.get(p.symbol)
        if m and m.get("action") == "close":
            force_close.add(p.symbol)
            new_positions.append(p)
            continue
        if m and m.get("action") == "hold" and m.get("new_stop") is not None:
            ns = float(m["new_stop"])
            mark = ctx.prices.get(p.symbol)
            # Trail only TIGHTER and short of the current mark — so a winning long can lock profit
            # ABOVE entry and a winning short BELOW entry (a stop past mark would insta-stop).
            tighter = mark is not None and (
                (p.direction == "long" and p.stop < ns < mark) or
                (p.direction == "short" and mark < ns < p.stop))
            if tighter:
                p = p.model_copy(update={"stop": ns})  # trail only; never loosen
                trailed += 1
        new_positions.append(p)
    positions = new_positions
```

Replace it with (adds the reduce branch + reduce accumulators; the close/hold branches are unchanged):

```python
    force_close, trailed = set(), 0
    reduced, banked_pnl, reduce_dropped = 0, 0.0, 0
    reduce_actions, reduce_warnings = [], []
    new_positions = []
    for p in positions:
        m = by_raw.get(p.symbol)
        if m and m.get("action") == "close":
            force_close.add(p.symbol)
            new_positions.append(p)
            continue
        if m and m.get("action") == "reduce":
            frac = _valid_reduce_fraction(m.get("reduce_fraction"))
            mark = ctx.prices.get(p.symbol)
            fr = ctx.fundings.get(ctx.raw_to_unified.get(p.symbol))
            spec = ctx.specs_by_raw.get(p.symbol)
            if frac is None or mark is None or fr is None or spec is None:
                reduce_dropped += 1
                new_positions.append(p)  # malformed/unpriceable reduce: leave the position whole
                continue
            n_events = count_funding_events(p.opened_ts, now, int(fr.interval_hours))
            res = reduce_position(p, mark, frac, funding_rate=fr.current_rate,
                                  funding_events=n_events, slippage_bps=_SLIPPAGE_BPS, spec=spec)
            if res.kind == "noop_dust":
                reduce_warnings.append(f"reduce noop (sub-lot) {p.symbol}")
                new_positions.append(p)
                continue
            if res.kind == "promote_full":
                force_close.add(p.symbol)  # runner would be dust -> full close via the normal path
                new_positions.append(p)
                reduce_actions.append({"reduce": p.symbol, "fraction": frac, "full": True})
                continue
            account.balance += res.closed_trade.realized_pnl  # bank the slice
            reduced += 1
            banked_pnl += res.closed_trade.realized_pnl
            reduce_actions.append({"reduce": p.symbol, "fraction": frac,
                                   "pnl": res.closed_trade.realized_pnl, "full": False})
            new_positions.append(res.runner)  # carry the reduced runner
            continue
        if m and m.get("action") == "hold" and m.get("new_stop") is not None:
            ns = float(m["new_stop"])
            mark = ctx.prices.get(p.symbol)
            # Trail only TIGHTER and short of the current mark — so a winning long can lock profit
            # ABOVE entry and a winning short BELOW entry (a stop past mark would insta-stop).
            tighter = mark is not None and (
                (p.direction == "long" and p.stop < ns < mark) or
                (p.direction == "short" and mark < ns < p.stop))
            if tighter:
                p = p.model_copy(update={"stop": ns})  # trail only; never loosen
                trailed += 1
        new_positions.append(p)
    positions = new_positions
```

- [ ] **Step 6: Attach the reduce report keys after `execute_proposals`**

In `gate_execute_step`, just after the existing block (lines 489–491):

```python
    report["dropped"] = dropped
    report["trailed"] = trailed
    report["halted"] = halted  # closed_by_review is set by execute_proposals (actual, not intent)
```

add:

```python
    report["reduced"] = reduced
    report["banked_pnl"] = banked_pnl
    report["reduce_dropped"] = reduce_dropped
    if reduce_actions:
        report.setdefault("actions", []).extend(reduce_actions)
    if reduce_warnings:
        report.setdefault("warnings", []).extend(reduce_warnings)
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `uv run pytest tests/test_orchestration.py -k reduce -q`
Expected: PASS (4 tests).

- [ ] **Step 8: Run the full orchestration suite (no regressions)**

Run: `uv run pytest tests/test_orchestration.py -q`
Expected: PASS (all prior tests still green).

- [ ] **Step 9: Commit**

```bash
git add futures_fund/orchestration.py tests/test_orchestration.py
git commit -m "feat(reduce): wire reduce action into gate_execute_step holdings review"
```

---

## Task 3: Cross-cutting coverage — telemetry, HALT, exposure

**Files:**
- Test: `tests/test_gate_wiring.py`
- Test: `tests/test_exposure.py`

- [ ] **Step 1: Write the failing tests (gate wiring: telemetry + HALT)**

Add to `tests/test_gate_wiring.py` (it imports the `test_orchestration` fixtures — reuse `_seed_holding`, `_settings`, `gate_execute_step`, `load_positions`; match the existing import style at the top of that file):

```python
def test_reduce_is_honored_on_halt(tmp_path):
    # a reduce is risk-DECREASING, so like a close it must still run under HALT
    import datetime as dt
    from futures_fund.orchestration import gate_execute_step
    from futures_fund.state import load_positions, set_halt
    from tests.test_orchestration import _seed_holding, _settings
    state_dir, memory_dir, ex = _seed_holding(tmp_path)
    set_halt(state_dir, True, reason="test halt")  # set_halt(state_dir, halt, reason="") — state.py:90
    report = gate_execute_step(
        ex, _settings(), state_dir, memory_dir, now=dt.datetime(2026, 3, 1, tzinfo=dt.UTC),
        cycle_no=2, proposals=[],
        management=[{"symbol": "ETHUSDT", "action": "reduce", "reduce_fraction": 0.5}])
    assert report["halted"] is True
    assert report["reduced"] == 1 and load_positions(state_dir)[0].qty == 0.5  # trim ran on halt
```

- [ ] **Step 2: Run it to verify it fails, then passes**

Run: `uv run pytest tests/test_gate_wiring.py -k reduce -q`
Expected: initially PASS already if Task 2 is correct (the loop runs regardless of halt). If it FAILS, the reduce branch is incorrectly gated by halt — fix by confirming the reduce branch sits in the unconditional holdings-review loop (it does in Task 2). This test is a guard, not a new feature.

- [ ] **Step 3: Write the failing test (exposure drops after a trim)**

Add to `tests/test_exposure.py`:

```python
def test_reduce_lowers_book_exposure(tmp_path):
    # trimming a leg halves its notional, so gross drops (notional = qty * mark)
    from tests.test_orchestration import _seed_holding, _settings
    from futures_fund.orchestration import gate_execute_step
    import datetime as dt
    state_dir, memory_dir, ex = _seed_holding(tmp_path)  # one ETH long, qty 1.0
    full = gate_execute_step(ex, _settings(), state_dir, memory_dir,
                             now=dt.datetime(2026, 3, 1, tzinfo=dt.UTC), cycle_no=2,
                             proposals=[], management=[{"symbol": "ETHUSDT", "action": "hold"}])
    gross_before = full["exposure"]["gross"]
    state_dir2, memory_dir2, ex2 = _seed_holding(tmp_path / "b")
    trimmed = gate_execute_step(
        ex2, _settings(), state_dir2, memory_dir2, now=dt.datetime(2026, 3, 1, tzinfo=dt.UTC),
        cycle_no=2, proposals=[],
        management=[{"symbol": "ETHUSDT", "action": "reduce", "reduce_fraction": 0.5}])
    assert trimmed["exposure"]["gross"] < gross_before  # half the qty -> ~half the gross
```

The gate report carries `exposure` with a `gross` key (set at the end of `gate_execute_step`; verified by `test_gate_wiring.py::test_gate_report_carries_post_trade_exposure`).

- [ ] **Step 4: Add the `management_review` passthrough test**

Add to `tests/test_management_review.py` (mirrors `test_populated_review_preserved`):

```python
def test_reduce_directive_preserved():
    review = [{"symbol": "ETHUSDT", "action": "reduce", "reduce_fraction": 0.5, "reason": "bank half"}]
    assert management_review({"management": review}) == review
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_gate_wiring.py tests/test_exposure.py tests/test_management_review.py -k "reduce or directive" -q`
Expected: PASS (3 tests).

- [ ] **Step 6: Commit**

```bash
git add tests/test_gate_wiring.py tests/test_exposure.py tests/test_management_review.py
git commit -m "test(reduce): halt-honored, exposure-drop, management_review passthrough"
```

---

## Task 4: Document the `reduce` action for the team

**Files:**
- Modify: `SKILL.md` (the held-management bullet, ~line 53)
- Modify: `agents/trader.md` (add a "How you think" bullet)

- [ ] **Step 1: Update `SKILL.md`**

Find this text in `SKILL.md` (end of the held-management bullet, ~line 53):

```
A profit-locked stop carries zero downside heat. v1 has no add/trim.
```

Replace with:

```
A profit-locked stop carries zero downside heat. **To bank part of a winner**, emit `{"symbol":"<raw>","action":"reduce","reduce_fraction":<0<f<1>,"reason":"..."}` — the gate closes that fraction at mark (realized PnL booked) and carries a smaller runner with the same thesis; if the leftover runner would fall below min-notional it is promoted to a full close. A `reduce` is a pure size cut (no stop change); to also trail the runner, send a `hold`+`new_stop` in a later review. There is still no ADD/scale-in.
```

- [ ] **Step 2: Update `agents/trader.md`**

In `agents/trader.md`, in the "How you think" section, immediately after the bullet that begins `- **Retire a decayed trigger via \`cancel_triggers\`.**`, add:

```
- **Bank part of a winner via `reduce`.** For a HELD position deep in profit, you may trim instead of fully closing: emit a management decision `{"symbol":"<raw>","action":"reduce","reduce_fraction":<0<f<1>,"reason":"..."}` to bank that fraction at mark and keep a smaller runner on the same thesis. Use it to lock realized profit while letting the rest run (e.g. bank half at +2R). It is symmetric for longs and shorts and is a pure size cut — to also tighten the runner's stop, use `hold`+`new_stop`. Never bank so much that the runner would be dust (the gate will just close it fully).
```

- [ ] **Step 3: Run the docs/conformance + full suite**

Run: `uv run pytest -q`
Expected: PASS — **498 passed** = 486 baseline + 12 new (`test_reduce.py` 5, `test_orchestration.py` reduce 4, `test_gate_wiring.py` 1, `test_exposure.py` 1, `test_management_review.py` 1). If the baseline has drifted from 486, the invariant is: every prior test still passes and exactly the 12 new tests are added.

- [ ] **Step 4: Commit**

```bash
git add SKILL.md agents/trader.md
git commit -m "docs(reduce): document the reduce holdings action for the Trader"
```

---

## Final verification

- [ ] Run the entire suite once more: `uv run pytest -q` — must be fully green (HARD rule: protected suite must pass before any commit).
- [ ] Confirm `git status` shows only the intended files: `futures_fund/reduce.py`, `futures_fund/orchestration.py`, `agents/trader.md`, `SKILL.md`, and the four touched test files. **No protected module** (`executor.py`, `exits.py`, `cycle.py`, `sizing.py`, `risk_gate.py`, `consolidation.py`, `policy.py`, `liquidation.py`) appears in the diff.
- [ ] Sanity-read the diff against the market-neutral rule: every reduce path is qty-based and direction-agnostic (PnL sign is internal to `close_at_mark`); no long/short asymmetry was introduced.

## Notes for the implementer

- **DRY:** the banked-slice PnL is never re-derived — `reduce_position` calls the protected `close_at_mark` on a fractional Position, so fees/funding-sign/slippage are guaranteed identical to a full close.
- **Journal continuity:** a `"reduced"` trim deliberately does NOT call `patch_outcome`/`record_outcome` — the runner keeps its `decision_id`/thesis and the decision is patched only on its eventual full close. A `"promote_full"` reduce flows through `force_close` so `execute_proposals` journals it exactly like any close. Do not add journaling to the reduce branch.
- **Heat:** no heat bookkeeping is needed — `execute_proposals` reserves heat on the post-reduce `positions` it receives, and heat/exposure are recomputed from qty each cycle.
- **YAGNI:** do not add `new_stop` on reduce, a fraction cap, `reduce_to_qty`, an auto-TP-ladder, or an RM-emitted reduce — all are explicitly out of scope for v1 (see spec).

# Fire-time Profit-Lock Ladder — Design

**Status:** approved (design + protected `cycle.py` edit authorized by user, 2026-06-16).

## Problem

A confirmation-gated `stop_entry` fires and opens a position *inside the gate run* — after the
Trader/management stage. So the position cannot receive a profit-lock trail until the **next**
management cycle. The trail is also **LLM-driven** (the RM/Trader must emit a `new_stop` each
cycle). Between the fire-cycle and the next management cycle, a freshly-fired winner is protected
only by its **original** stop. If a cycle is missed (loop/cron outage, machine restart, crash) a
deep-in-profit position can round-trip the whole move back through that original stop.

**Concrete loss (cy97→cy98):** BTC fired a clean gated breakout, ran to **+1.6R** unrealized, then
a ~12h loop outage meant no profit-lock trail ran; the un-managed position round-tripped and
stopped at **−1.12R (−$86.73)**. The native resting stop capped catastrophe, but the *profit-lock
trail* needs the orchestrator and it did not run. (cy98 Reflector lesson, importance 8.)

## Goal

A **deterministic** profit-lock ladder, evaluated by the gate **every cycle it runs (including the
fire cycle)**, that ratchets a position's stop toward profit **up-only / tighten-only** based on the
position's R-multiple favorable excursion — with **no LLM involvement**. A position that is in
profit at the last gate-run before an outage is then protected by a ratcheted stop on the next run.

## Scope

**In scope (this spec):** the deterministic fire-time ladder (covers the BTC case and any position
in profit at the last gate-run before an outage).

**Out of scope (deferred to a separate spec):** *missed-candle replay* — having the gate, on resume
after a gap, replay the skipped candles gap-honestly and ratchet across them. That would also cover
the rarer case where a position rises to profit *entirely within* an outage (no gate run in
between). It changes the protected candle-serving model and is larger/riskier; not needed for the
observed loss. **Not built here.**

## Architecture

Three pieces; minimal protected footprint.

### 1. `futures_fund/profit_lock.py` — NEW, non-protected, pure

The ladder logic, fully unit-testable, no IO.

```python
# Default ladder: ordered (trigger_R, lock_R) rungs. Conservative — room early, lock progressively.
LADDER_RUNGS: list[tuple[float, float]] = [
    (1.0, 0.0),   # +1.0R reached -> stop to breakeven (entry)
    (1.5, 0.5),   # +1.5R reached -> stop to +0.5R
    (2.0, 1.0),   # +2.0R reached -> stop to +1.0R
    (3.0, 2.0),   # +3.0R reached -> stop to +2.0R
]

def ladder_stop(direction, entry, entry_stop, favorable_price, rungs=LADDER_RUNGS) -> float | None:
    """The highest profit-lock stop the favorable excursion has UNLOCKED, or None if no rung is
    reached. Pure. R-unit is anchored to the ORIGINAL risk (|entry - entry_stop|), not the
    (possibly already-ratcheted) live stop, so the ladder is stable across ratchets.
      R_unit = abs(entry - entry_stop)               # original per-unit risk
      fav_R  = (favorable_price - entry)/R_unit       # long;  (entry - favorable_price)/R_unit short
      rung   = highest (trigger_R, lock_R) with trigger_R <= fav_R
      stop   = entry + lock_R*R_unit (long) / entry - lock_R*R_unit (short)
    Returns None when: no rung reached, R_unit<=0, or favorable_price non-finite."""
```

Behavioral contract:
- **Up-only by construction at the rung level:** a higher `favorable_price` selects an equal-or-
  higher rung, never a lower one. (Monotonic non-decreasing in favorable excursion.)
- `lock_R` values are `>= 0` in the default ladder, so the proposed stop is always at/above
  breakeven — the ladder never proposes a *losing* stop. (A future config could include a negative
  early rung; the tighten-only wiring guard below makes even that safe.)
- Direction-symmetric (mirror for short).

### 2. `cycle.py` wiring — PROTECTED (the one authorized edit)

**As-built placement (refined during implementation):** the ratchet lives in `execute_proposals`'
OPEN loop, immediately after `open_position`, and runs **only on freshly-opened positions** — i.e.
true *fire-time*. This is stronger than the spec's first sketch (a lazy set in `audit_and_reflect`
before `detect_exit`, which runs *before* fires and so would not see a freshly-fired position until
the next cycle — by which point the firing-candle excursion is gone). Critically, it does **not**
touch carried positions, so the RM's deliberate manual trail is never overridden.

```python
pos = pos.model_copy(update={"liq_price": liq})           # freshly-opened position
_orig_stop, _ratchet = pos.stop, None                     # the ORIGINAL stop at open
fb = last_completed_frame(ctx.frames[unified], now, tf).iloc[-1]   # the FIRING candle
_ratchet = ratcheted_stop(pos.direction, pos.entry, _orig_stop, _orig_stop,
                          fb.high, fb.low, fb.close)        # tighten-only via mark=close
pos = pos.model_copy(update={"entry_stop": _orig_stop,
                             "stop": _ratchet if _ratchet is not None else _orig_stop})
if _ratchet is not None:
    report["profit_locks_ratcheted"] += 1
```

The canonical tighten-only rule moves to `profit_lock.is_tighter_stop`; `orchestration._is_tighter_stop`
delegates to it (DRY — one definition shared by the LLM trail and the ladder).

- **Safety invariant:** the ratchet is applied *only* through the existing
  `_is_tighter_stop(direction, cur_stop, new_stop, mark)` rule (long: `cur_stop < new_stop < mark`;
  short mirror). It can **only tighten** the stop toward profit and must be short of the mark — it
  **never loosens a stop, never widens risk, never moves a stop past the mark** (which would
  insta-stop). This *strengthens* the protected safety path; it weakens nothing.
- **Composes with the LLM manual trail:** the RM/Trader may still emit a `new_stop`; both go through
  `_is_tighter_stop`, so the tighter of {ladder, manual} wins. No conflict, no ordering hazard.
- **Gap-honest fills unchanged:** the ladder only sets `position.stop`; `detect_exit` /
  `_gap_honest_exit_level` fill it exactly as today (a stop the bar gapped past fills at the worse of
  level/bar-open). No change to fill realism.
- `_is_tighter_stop` and `ladder_stop` are imported into `cycle.py`; the block sits in the existing
  per-open-position loop immediately before the `detect_exit` call.

### 3. `Position.entry_stop: float | None = None` — `state.py`, non-protected

Stores the original stop so the ladder's R-unit stays anchored to the original risk after the live
`stop` ratchets. Set lazily on first ladder evaluation (`if entry_stop is None: entry_stop = stop`)
— on the fire cycle the live stop *is* the original (no ratchet has happened yet), so no
`executor.open_position` edit is needed. Defaulting to `None` is backward-compatible (the book is
currently flat, so there is no open-position migration; any future None just self-heals on first
sight). `entry_stop` is informational for risk reporting too (the original per-unit risk).

## Data flow

```
fire cycle (execute_proposals OPEN loop), per FRESHLY-OPENED position:
  open_position -> stop = original (= the gated proposal stop, no trail yet)   [unchanged]
  -> ladder ratchet (NEW): entry_stop = original; candidate = ratcheted_stop(firing candle);
     if tighter-and-short-of-close: stop = candidate   -> persisted via save_positions
NEXT cycle (audit_and_reflect), per CARRIED position:
  detect_exit(position, bar_high, bar_low, ...) uses the PERSISTED (ratcheted) stop  [unchanged]
  -> a pullback through the profit-lock exits at the lock (a WIN), not the original stop
```

The fire-cycle ratchet captures the firing candle's favorable excursion immediately and persists the
tightened stop, so the very next exit check (even after a missed-cycle outage) stops at the
profit-lock — exactly the behavior that would have protected the cy97 BTC position (+0.5R win instead
of −1.12R). Carried positions are the RM's to manage; the ladder does not re-touch them.

## Error handling / edge cases

- `entry_stop is None` → self-heal to current stop (first sight).
- `R_unit <= 0` (degenerate: entry == entry_stop) → `ladder_stop` returns None → no ratchet.
- `favorable_price` non-finite / missing → return None → original stop stands (fail-safe).
- Ratchet that is not strictly tighter, or not short of the mark → rejected by `_is_tighter_stop`
  (no-op). The advisory noise-band warning is *not* applied to the ladder (the ladder's levels are
  R-derived structural profit-locks, not discretionary prices; the manual-trail noise-band guard is
  unchanged and still advisory-only).
- Multiple open positions → evaluated independently per position.

## Testing

- **`tests/test_profit_lock.py`** (pure): each rung unlocks at exactly its `trigger_R`; below the
  first rung returns None; monotonic up-only (a lower favorable excursion never returns a lower
  stop); short mirror; degenerate `R_unit<=0` → None; non-finite favorable → None; the exact
  breakeven level at the +1R rung.
- **Wiring / regression test** (in the gate/cycle test suite): reproduce the cy97 BTC scenario — a
  long fires with entry 66495.9 / stop 66046.8, the firing candle's high reaches ~+1.6R, the ladder
  ratchets `stop` to the +0.5R rung (or breakeven), and a subsequent candle whose low pulls back
  through that ratcheted stop but stays above the *original* stop exits at the **profit-lock** (a
  small win), **not** at −1.12R. Assert `profit_locks_ratcheted >= 1` and the realized PnL is
  positive.
- Existing `detect_exit` / trail / gate tests must remain green (the ladder only adds tightening).
- Full `uv run pytest` green; zero net-new ruff on the touched files.

## Non-goals / invariants

- No change to `executor`, `exits`, `risk_gate`, `consolidation`, `liquidation`, `sizing`.
- No change to fill realism (gap-honest exits unchanged).
- The ladder can only *strengthen* protection (tighten-only). It is impossible for the ladder to
  widen risk or move a stop to the loss side of where it already is.
- Defaults are conservative and tunable in one place (`LADDER_RUNGS`).

## Rollout

TDD task-by-task (writing-plans), full suite green per task, adversarial review (independent
verification of the tighten-only invariant + the regression) before any commit. Direct-to-main per
the session workflow. Commit trailer: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.

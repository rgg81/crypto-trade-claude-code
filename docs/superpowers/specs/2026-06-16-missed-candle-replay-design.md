# Missed-candle Replay (aggregate-window, exits-only) — Design

**Status:** approved (design + protected `audit_and_reflect` edit authorized by user, 2026-06-16).

## Problem

The exit check reads exactly one bar: `last_completed_frame(df, now).iloc[-1]` — the latest completed
4h candle. During a loop outage (the gate doesn't run for several boundaries) the desk, on resume,
serves only the *latest* candle and silently discards the intermediate **missed** candles. So a
stop / take-profit / liquidation that price actually touched *during* a missed candle is **not
honored** if price recovered by the latest candle: the position wrongly stays open (or an exit is
recorded at the wrong level). The profit-lock ladder (#268) protects a freshly-fired winner's stop,
but it does not make exits *fire* across a gap. This is the deferred Problem-B sibling of #268.

## Goal

When the gate resumes after a gap, the exit check must consider **every completed candle since the
last-served candle**, not just the latest — so any stop/TP/liq touched in the gap is honored.

## Scope

**In scope:** EXITS only (stop/TP/liq on open positions), via an **aggregate window** (the gap's
`max(high)`, `min(low)`, and the first missed bar's open). The existing pessimistic priority
(liq > stop > tp) and gap-honest fill model are reused unchanged.

**Out of scope:** (a) firing *pending stop_entry triggers* that should have fired mid-gap; (b)
*bar-by-bar ordered* replay (which exit came first). The aggregate window is conservative — if both a
TP and a stop were touched in the gap it records the **stop** (pessimistic, never an optimistic paper
win), consistent with the desk's existing single-bar exit model. Deferred to separate specs.

## Architecture

Minimal protected footprint: one edit to `audit_and_reflect`; everything else non-protected.

### 1. `futures_fund/replay.py` — NEW, non-protected, pure

```python
def gap_window(df, last_served_ts, now, timeframe="4h", direction="long") -> tuple | None:
    """The (max_high, min_low, gap_open) over the COMPLETED candles with open-ts >= last_served_ts
    and up to `now`'s completed bar — the bars the gate missed during a gap. `gap_open` is the
    DIRECTIONALLY-CONSERVATIVE open for a gap-honest stop fill: the MIN open across the window for a
    long, the MAX for a short (see the conservatism note). Returns the SINGLE latest completed bar's
    (high, low, open) when there is no gap (one new bar), when `last_served_ts` is None/stale (>= the
    latest completed ts), or when the frame is too short — byte-identical to today. None only if
    there is no completed bar."""
```

Logic: drop the still-forming bar (reuse `last_completed_frame`); take the rows with open-ts
**`>= last_served_ts`** (see the off-by-one note below); if none (no gap / stale floor) use just the
latest completed row; return `(max of high, min of low, directionally-conservative open)`. Pure, no
IO.

**Why a directional `gap_open`, not the earliest open (the conservatism fix from the adversarial
review).** `_gap_honest_exit_level` fills a long stop at `min(level, bar_open)` and a short stop at
`max(level, bar_open)`. If `gap_open` were the EARLIEST missed bar's open, then when a *later* bar is
the one that gaps through the stop, `min(level, earliest_open)` pins the fill to the unreachable stop
LEVEL — booking a smaller loss than reality (an **optimistic paper win**, violating the desk's
pessimistic fill model). The most pessimistic open is the **minimum** across the window for a long
(the bar that gapped down furthest) and the **maximum** for a short. This is also exactly honest:
any open below a long's stop belongs to a bar that genuinely gapped through it, so `min(level,
min_open)` is the true worst-case fill, never an over-penalty. On the no-gap path the window is one
bar whose single open is direction-invariant, so identity holds. The caller passes `p.direction`.

**Why `>=`, not `>` (the off-by-one this feature exists to kill).** A cycle that ran at instant
`T` evaluated the bar with open-ts `floor4(T) - 4h`: `last_completed_frame` drops the then-forming
served-candle bar (open-ts `floor4(T)`), leaving `floor4(T) - 4h` as the last *completed* bar. The
served candle stamped in its report is `S = floor4(T)`. So the bar opening **exactly at `S`** was
still forming during that run and was **never evaluated** — it is the FIRST missed bar. The
un-evaluated set is `open-ts in (S - 4h, B_cur_last]`, which on the 4h grid is `open-ts >= S`. Using
`>` would start at `S + 4h` and silently skip that first missed candle. The no-gap case still
reduces to the single latest bar: with `S` one step back, `open-ts >= S` selects exactly the one
new completed bar → byte-identical to today.

### 2. `futures_fund/scheduling.py` — non-protected, reuse existing served-candle logic

```python
def last_served_candle(state_dir, now) -> datetime | None:
    """The served candle of the most-recent COMPLETED cycle (highest cycle dir whose report.json
    parses), via the existing descending scan + _served_candle. None if no completed cycle. This is
    the floor of the gap window — the last candle the gate actually processed before this run."""
```

At the moment `audit_and_reflect` runs, the current cycle's `report.json` does not yet exist, so this
returns the PRIOR cycle's served candle — exactly the gap floor. A DUE RETRY (no report.json stamped)
is skipped by the scan, so the retry re-processes from the same prior candle (idempotent).

### 3. `cycle.py` `audit_and_reflect` — PROTECTED (the one authorized edit)

Add an optional `last_served_ts: datetime | None = None` parameter. Replace the single-bar extraction

```python
cdf = last_completed_frame(ctx.frames[sym], now, ctx.settings.timeframe)
bar = cdf.iloc[-1]
# detect_exit(p, bar_high=float(bar["high"]), bar_low=float(bar["low"]), bar_open=float(bar["open"]), ...)
```

with the gap-window aggregate:

```python
win = gap_window(ctx.frames[sym], last_served_ts, now, ctx.settings.timeframe)
if win is None:
    still_open.append(p); continue          # no completed bar -> nothing to check (as today)
g_high, g_low, g_open = win
ct = detect_exit(p, bar_high=g_high, bar_low=g_low, bar_open=g_open, ...)
```

`detect_exit` and `_gap_honest_exit_level` are **unchanged**. With `last_served_ts=None` or no gap,
`gap_window` returns the single latest bar, so behavior is identical to today. The change can only
**widen** the [low, high] the exit is checked against — it can surface an exit today silently missed,
never suppress one. `ctx.prices` (live last row) still drives MTM equity, unchanged.

### 4. Caller — `orchestration.preflight_step` (non-protected)

`audit_and_reflect` is invoked from `preflight_step` (Phase 0-2 — load state, audit exits BEFORE the
halt check), NOT `gate_execute_step` (the spec's first sketch named the wrong function). Compute
`last_served_ts = scheduling.last_served_candle(state_dir, now)` there and pass it into
`audit_and_reflect`. `cycle.run_cycle` (the baseline/test path) keeps passing the default `None`
(single-bar, today's behavior) — a no-op change.

## Edge cases / invariants

- No gap (normal cadence, exactly one new completed bar) → window = that bar → identical to today.
- `last_served_ts` None / >= latest completed ts (stale/clock-skew) → latest single bar (fail-safe).
- DUE RETRY (no report yet) → floor is the prior cycle's candle → re-processes the same window
  idempotently (a position already closed by a prior partial run is gone from `positions.json`, so no
  double-close).
- Pessimistic and conservative: the window's `min_low`/`max_high` with liq>stop>tp can only record an
  exit at/worse-than a single-bar check; never an optimistic win. The gap-honest FILL is likewise
  pessimistic — `gap_open` is the directionally-worst window open (min long / max short), so a stop
  gapped by a LATER missed bar fills at that worse open, not the unreachable stop level (the
  adversarial-review fix). Funding is unchanged (accrues over the whole hold via
  `count_funding_events(opened_ts, now)`).
- A position opened THIS cycle is opened in `execute_proposals` AFTER `audit_and_reflect`, so it is
  never gap-replayed on its open cycle (correct — it had no prior served candle to gap from).

## Testing

- **`tests/test_replay.py`** (pure): no-gap → single bar; a 2- and 3-bar gap → correct
  `max_high`/`min_low`; **the directional gap-open pin — a long takes the MIN window open, a short
  the MAX, even when a LATER bar (not the earliest) is the one that gapped** (the conservatism fix);
  **the off-by-one pin — a 1-candle gap includes BOTH the
  bar that opened AT `last_served_ts` (forming during the prior run) AND the latest bar** (`>=`, not
  `>`); `last_served_ts` None/stale → latest bar; forming-bar dropped; short/empty frame safe.
- **`tests/test_scheduling.py`** (`last_served_candle`): cold start → None; returns the highest
  parsing report's served candle; skips a current dir with no/garbled report (idempotent RETRY).
- **Regression** (gate/cycle): a long whose stop sits between the latest bar's low and a *missed*
  bar's low — i.e. the missed bar dipped through the stop but price recovered so the latest bar's low
  is above it — now **exits at the stop** (asserting it does NOT stay open). The counterfactual
  (single-bar today) leaves it open. Plus a no-gap test and a cold-start test asserting unchanged
  behavior, and a **fill-price** test: a missed bar that GAPS OPEN below the stop exits at the gapped
  open (realized loss reflects the gap, not the stop level), pinning the directional `gap_open` fix.
- Full `uv run pytest` green; zero net-new ruff on touched files; existing exit/cycle/orchestration
  tests unaffected (no-gap path is identical).

## Non-goals / invariants

- No change to `exits`, `executor`, `risk_gate`, `liquidation`, `sizing`, `consolidation`. Fill
  realism is unchanged in MECHANISM (`_gap_honest_exit_level`); the only change is feeding it the
  window's directionally-worst open instead of a single bar's — strictly more pessimistic.
- The replay can only ADD a missed exit, never suppress/relax one — it strengthens fidelity.
- Conservative (pessimistic) by construction; no optimistic paper wins.
- **Known pre-existing limitation (NOT introduced here, NOT fixed here):** the gap floor is the
  prior cycle's *served candle* `S = floor4(gate-execute instant)`, but the audit runs at the
  earlier *preflight* instant. If a prior cycle's funnel straddled a 4h boundary (preflight in candle
  `C`, gate stamped `S = C+4h`), the floor `>= S` can skip bar `C`. The OLD single-bar code missed
  that same bar identically, so the feature remains a strict superset; hardening would derive the
  floor from the preflight audit instant (touches the protected scheduling cadence primitive) —
  deferred.

## Rollout

TDD; full suite green; adversarial review (the conservatism + no-gap-identity invariants, the
regression faithfulness) before any commit. Direct-to-main. Trailer:
`Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.

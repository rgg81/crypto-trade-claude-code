# Stale-trigger auto-revalidation — design

**Goal:** Auto-cancel an armed `stop_entry` trigger whose swing anchor has drifted PAST its level
between cycles, so a mis-leveled breakout/breakdown trigger can no longer fire mid-bounce. The team
re-arms at the true level next cycle via normal flow. Symmetric long/short. Fail-safe. Reported.

## The problem (cy43 ETH, real numbers)

A `stop_entry` SHORT (flush-short / breakdown) fires on a completed-bar `close < trigger_level`. To
be a *break to new lows*, `trigger_level` must sit at/below current support (`swing_low`). When the
swing_low FALLS below the trigger level, the trigger is stranded ABOVE support — a close through it
is a mid-range bounce-failure, **not** a fresh breakdown. The desk caught this by eye in cy43 (RM
canceled + re-armed); we want the team to handle it deterministically.

Real cy43 ETH: `trigger_level=1532`, `swing_low=1503.6`, `atr≈64.1`, `last_close=1574.54`.
Drift `1532 − 1503.6 = 28.4 = 0.44·ATR` (1.85% of level). Price is ABOVE the trigger, support is
BELOW it → firing here = mid-bounce. STALE.

## The invariant (symmetric) — CROSSING detection, not current geometry

A naive "swing is currently on the wrong side of the level" rule over-cancels: it cannot tell the
cy43 *drift* (a trigger that WAS a valid breakdown anchor, then the swing crossed it) from a trigger
deliberately placed away from the 20-bar swing (a legit tighter-level entry). Auto-canceling the
latter kills valid trades — against the loosen-execution mandate. So we detect the **crossing** using
the swing captured at ARM time (`anchor_swing`), against a single deadband line `L`:

For `stop_entry` only (limit_entry is a pullback TOUCH — opposite geometry, never judged stale), and
ONLY when `anchor_swing` was recorded (unstamped → never revalidated):

- **short (breakdown):** `L = trigger_level − buffer`; **stale iff `anchor_swing ≥ L` AND `swing_low < L`** (support was at/above the line at arm and has since crossed below it).
- **long (breakout):** `L = trigger_level + buffer`; **stale iff `anchor_swing ≤ L` AND `swing_high > L`** (resistance was at/below the line at arm and has since crossed above it).

The `anchor_swing ≥/≤ L` clause is the discriminator: a trigger never anchored at/beyond the swing
(a tighter-level entry) has `anchor_swing` on the far side of `L` → never flagged. The swing is the
SAME `swing_levels(df, lookback=20)` (highest-high / lowest-low over 20 completed bars) the brief
feeds the analysts. A short's swing_low only crosses below `L` when a genuine NEW lower low prints —
structurally meaningful, not noise (aging-out RAISES swing_low, away from stale). Mirror for longs.

### Buffer (deadband)

`buffer = max(STALE_TRIGGER_ATR_FRAC · atr, STALE_TRIGGER_PCT_FALLBACK · |trigger_level|)`

- `STALE_TRIGGER_ATR_FRAC = 0.25` — grounded in cy43: drift was `0.44·ATR`, so `0.25·ATR=16.0`
  catches it with margin while staying well above tick wobble. `0.5·ATR=32.0` would MISS it.
- `STALE_TRIGGER_PCT_FALLBACK = 0.0025` — used only when `atr` is missing/zero/NaN, so there is
  always a finite deadband.

## Fail-safety (never cancel a still-valid trigger)

- **Unstamped** (`anchor_swing is None`) → **NEVER revalidated** (legacy triggers, hand-built test
  orders, non-swing-anchored levels). Auto-cancel can only retire a trigger that recorded a real
  arm-time anchor and has since been crossed.
- Missing / non-finite (`None`/NaN/inf) `anchor_swing`, current swing, OR trigger_level → not stale.
- A symbol with no swing entry (feed gap) → kept.
- `limit_entry` and any non-`long`/`short` direction → never stale.
- Auto-cancel only ever REMOVES a crossed trigger; it never opens a position and never re-arms (the
  team re-arms via judgment). Strictly a safety reduction — weakens no limit.

## Mechanics — where it runs

1. **`futures_fund/pending_orders.py`** (non-protected): a `PendingOrder.anchor_swing: float | None`
   field; `_stale_geometry(order, swing_high, swing_low) -> bool` (the crossing invariant above);
   `revalidate_triggers(orders, swings_by_symbol) -> (stale, healthy)` partitioning armed orders
   (`swings_by_symbol` maps RAW symbol → `(swing_high, swing_low)`). No change to
   `check_pending_orders`.

2. **`futures_fund/orchestration.py` `gate_execute_step`** (non-protected) — in the existing
   per-symbol loop where `df = last_completed_frame(...)` is already loaded, compute `swing_levels(df)`
   for every symbol (cheap; needed both to STAMP new triggers and to revalidate old ones). After
   `check_pending_orders` returns `(fired, expired, remaining)`, compute `stale` over `fired +
   remaining` via `revalidate_triggers`, then drop stale ids from BOTH `fired` (never opened) and
   `remaining` (never persisted) — the same effect as an explicit `cancel_triggers`, no extra store
   write, no `check_pending_orders` change. Only PRIOR-armed triggers are revalidated; this cycle's
   `new_triggers` are placed against this swing and are fresh by construction.

3. **Stamping (`_stamp_anchor_swing` helper)** — applied identically to BOTH provenances: the Trader
   `triggers` param AND the counter-regime `cr_armed` conversions, so neither is left silently
   un-revalidatable (no provenance gap). Directional (swing_low for short, swing_high for long),
   no-op for non-stop_entry / already-stamped / absent-swing. The feature is **self-priming**: it
   acts on triggers armed after it ships; a pre-existing unstamped trigger is grandfathered out
   (never auto-canceled), and is NOT backfilled (that would hand-edit `state/`, Rule 3).

4. **Report** — `auto_canceled_stale` (count, distinct from `triggers_canceled`) and an
   `actions`/`warnings` line per stale cancel so the orchestrator surfaces it (Rule 6).

## Tests (TDD)

- `_stale_geometry`: cy43 ETH numbers (anchor 1538, now 1503.6) → stale; support not crossed →
  healthy; UNSTAMPED → never stale; long mirror; within-buffer wobble → not stale; limit_entry never
  stale; tighter-level short (anchor below line) not flagged; missing/NaN/inf anchor/swing/level →
  not stale; pct-floor when atr=0.
- `revalidate_triggers`: partitions; symbol with no swing entry → healthy.
- gate wiring: a prior-cycle stale short is auto-canceled even when it WOULD fire (not opened, not
  persisted, reported); long mirror; an unstamped prior trigger is never auto-canceled; a new Trader
  stop_entry is stamped (self-priming); a counter-regime conversion is stamped (provenance parity).

## Out of scope (YAGNI)

Auto-RE-ARM at the corrected level (a team judgment — Rule 1) and limit_entry staleness. (The
crossing design DOES require a stored arm-time anchor — the originally-considered current-swing-only
comparison was rejected because it over-cancels valid tighter-level entries.)

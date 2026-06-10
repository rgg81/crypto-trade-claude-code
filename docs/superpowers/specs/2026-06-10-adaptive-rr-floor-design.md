# Adaptive, reflection-driven RR floor — design spec

**Date:** 2026-06-10 · **Status:** approved (design), pending implementation plan

## Goal

Replace the gate's single hard `MIN_RR = 2.0` reward:risk floor with a **per-regime-quadrant
floor that adapts automatically**, tuned by the reflection loop from the desk's own shadow-veto
outcomes — never hand-adjusted — and clamped to a hard, gate-enforced safety band `[1.6, 2.5]`.

**Motivation (cy68):** the desk's ~13-cycle deployment drought is substantially explained by the
2.0 floor vetoing the only setups the chop offers (RR 1.6–1.9). In low-vol/range regimes a 2R target
is structurally hard; in strong trends 2R+ is easy and the desk should be *more* selective. A fixed
floor cannot express that. cy68 HYPE (1.84) and SOL (1.64) fired and were both RR-vetoed.

## Non-negotiable constraints

- **Safety-first / protected-module spirit.** Two protected modules are edited (`risk_gate`,
  `cycle`) — minimally, and **only because the gate is the authority that vetoes**, so the adaptive
  floor must land there or trades are still vetoed at fire. The gate retains a **gate-owned absolute
  hard minimum** (`HARD_MIN_RR = 1.6`): the floor can never drop below it, even if `rr_floor.json`
  is missing, corrupt, or hostile. This is a **user-authorized** relaxation of the RR limit (from a
  fixed 2.0 to an adaptive `[1.6, 2.5]`); it is flagged here for the record.
- **Seed = today's behavior.** All four quadrant floors initialize at 2.0, so day-1 behavior is
  byte-identical to the current desk. The floor only *earns* its way looser from evidence.
- **Safety-asymmetric adaptation.** Slow to loosen, fast to tighten (see §4).
- **Fail-safe.** Missing/corrupt floor state → all quadrants default to 2.0 (today's behavior).
- **No new long/short asymmetry** (the floor is per-quadrant, direction-agnostic).

## Architecture (6 units)

> **Scoring note (vigilance):** `shadow_outcome` today is **one-bar** and **currently unused** —
> there is no existing scoring loop. One-bar scoring is too sparse and biases toward fast resolvers,
> so this design adds **multi-bar first-touch** scoring (§6a) with a resolution cache, rather than
> tuning a safety limit off a biased single-bar sample.

### 1. Floor state — `state/rr_floor.json` (new runtime state)
```json
{"high_vol_trend": 2.0, "low_vol_trend": 2.0, "high_vol_range": 2.0,
 "low_vol_range": 2.0, "updated_cycle": 0}
```
Four per-quadrant floors + provenance. Written ONLY by the reflection phase (never hand-edited —
HARD Rule 3). Read by the gate path and the CP9 arm-guard.

### 2. `futures_fund/rr_floor.py` (NEW, non-protected) — pure logic
- `BAND = (1.6, 2.5)`; `SEED = 2.0`; `QUADRANTS = (high_vol_trend, low_vol_trend, high_vol_range, low_vol_range)`.
- `load_rr_floor(state_dir) -> dict` — read JSON; fail-safe to all-SEED on missing/corrupt/partial
  (any missing quadrant key → SEED for that key).
- `effective_rr_floor(quadrant, state) -> float` — `clamp(state.get(quadrant, SEED), *BAND)`.
- `adapt_rr_floor(state, scored_by_quadrant, cycle_no) -> (new_state, changes)` — the §4 nudge.
  Pure; returns the updated dict + a list of human-readable change descriptions.

### 3. Gate change — `risk_gate.py` (PROTECTED, minimal)
- `GateInputs` gains `rr_floor: float | None = None` (default None ⇒ legacy `MIN_RR`).
- `HARD_MIN_RR = 1.6` constant (the gate-owned absolute floor).
- In `evaluate`, the RR check becomes:
  `floor = max(inputs.rr_floor if inputs.rr_floor is not None else MIN_RR, HARD_MIN_RR)`
  then veto if `rr < floor - _RR_EPS` (same epsilon as today). `MIN_RR = 2.0` stays as the default.
  The `max(..., HARD_MIN_RR)` wrap is what guarantees a corrupt/hostile adaptive floor can never
  breach the absolute safety bound.

### 4. Gate wiring — `cycle.py::execute_proposals` (PROTECTED, minimal)
- `state_dir` is already a parameter. Load `floor_state = load_rr_floor(state_dir)` ONCE per call.
- Per proposal: `rr_floor = effective_rr_floor(simple_regime(ctx.frames[unified]).quadrant, floor_state)`
  and pass it into `GateInputs(..., rr_floor=rr_floor)`. (`simple_regime(...)` is already computed
  on the same frame at this site, so the quadrant is free.)

### 5. CP9 arm-time guard — `orchestration.py` (non-protected, already exists)
The cy68 arm-time RR guard currently uses the constant `MIN_RR`. Change it to use the per-trigger
regime-adaptive floor: for each new trigger, `effective_rr_floor(quadrant_of(trigger), floor_state)`
where the quadrant comes from the trigger's symbol frame (the same `swings_by_symbol` source already
computed in the gate step). Arm-time and fire-time floors must agree (else a trigger arms then
vetoes — the exact bug cy68 surfaced). Refuse-only, unchanged otherwise.

### 6a. Shadow scoring + resolution cache — `shadow.py` (non-protected) + `state/shadow-scored.json`
The vetoed-entry dict (built in `cycle.execute_proposals`, the §4 protected edit) gains a `quadrant`
field and a stable `id`. A new `score_shadow_first_touch(entry, bars)` walks the symbol's bars from
the veto forward (capped at horizon `H = 12` bars ≈ 2 days) and returns the **first-touch** outcome:
`won` (TP touched before stop), `lost` (stop touched before TP / `veto_saved`), `pending` (neither
yet, < H bars elapsed), or `expired` (H bars elapsed, neither touched). Bars come from the OHLCV the
desk already loads (`ctx.frames` for symbols in the cycle's universe; the reflect phase scores any
entry whose symbol's frame is available and skips the rest until it reappears). Each entry is
resolved **exactly once** into `state/shadow-scored.json` (`{id: {outcome, quadrant, cycle}}`);
`won`/`lost`/`expired` are terminal, `pending` is re-checked next cycle. `expired` is excluded from
the rate (no first-touch signal).

### 6b. Reflection adaptation — `rr_floor.adapt_rr_floor` + reflect wiring (non-protected)
Each reflect phase, after §6a updates the cache:
1. Tally terminal `won`/`lost` resolutions by quadrant from `state/shadow-scored.json` (only RR-veto
   entries — those carry a quadrant; legacy entries without one are skipped, the loop is
   forward-looking).
2. For a quadrant with **≥ N_MIN = 8 decided** (won+lost) samples, compute `w = won / (won + lost)`:
   - `w > 0.60` (vetoes are costing winners) → **loosen** that floor by `−0.05` (small step).
   - `w < 0.40` (vetoes are saving losers) → **tighten** by `+0.10` (larger step).
   - `0.40 ≤ w ≤ 0.60` → dead-band, no change (hysteresis).
3. Use a **trailing window** (the most recent `W = 40` decided samples per quadrant) so the floor
   tracks the *current* regime behaviour, not the all-time average.
4. Clamp every quadrant to `[1.6, 2.5]`; bump `updated_cycle`; write `state/rr_floor.json`.
5. Emit a surfaced action/warning per change:
   `rr_floor low_vol_range 2.00→1.95 (vetoes cost 6/8 winners, w=0.75)`.

The asymmetry (−0.05 loosen vs +0.10 tighten) and the 8-sample minimum + dead-band make the loop
deliberately slow to relax the limit and quick to re-tighten when the regime turns.

## Data flow

```
preflight → analysts → … → gate_execute_step:
    execute_proposals loads rr_floor.json, threads per-quadrant floor into GateInputs → evaluate
      (RR-vetoes append to shadow-ledger.jsonl WITH quadrant + id)
    CP9 arm-guard uses the same effective floor
  → reflect: §6a first-touch score new/pending shadow entries from ctx.frames → shadow-scored.json
            §6b tally won/lost by quadrant (trailing W=40) → adapt_rr_floor → write rr_floor.json
```
The floor a cycle uses is always the previous cycle's learned state (one-cycle lag — acceptable; the
floor moves slowly by design).

## Error handling / vigilance

- Missing/corrupt/partial `rr_floor.json` → all-SEED (2.0) = today's behavior. Never throws.
- A quadrant with < 8 samples → never moves (no acting on noise).
- The gate's `max(..., HARD_MIN_RR)` wrap means even a `rr_floor.json` hand-corrupted to `{"x":0.1}`
  cannot drop any veto below 1.6.
- Every floor change is surfaced (Rule 6). The orchestrator additionally flags if a quadrant **pins
  to a bound for ≥ 5 consecutive updates** (a signal the regime model or the adaptation logic is off
  — surfaced to the user, not silently absorbed).

## Testing (TDD)

Unit (`rr_floor.py`): clamp to band; load fail-safe (missing file, corrupt JSON, partial keys);
`adapt_rr_floor` — loosen on w>0.6, tighten on w<0.4, dead-band 0.4–0.6, no-move under 8 samples,
clamp at both bounds, asymmetric step sizes, trailing-window W. Scoring (`shadow.py`):
`score_shadow_first_touch` — TP-before-stop=won, stop-before-TP=lost, neither<H=pending,
neither=H→expired, long/short mirror, first-touch order respected within a bar (ambiguous
same-bar TP+stop resolves to `lost`, the conservative assumption). Gate (`risk_gate`): a 1.7-RR
proposal with `rr_floor=1.6` passes; `rr_floor=None` (→2.0) vetoes; a corrupt `rr_floor=0.5` is
wrapped to 1.6 and still vetoes a 1.5-RR trade (hard-min holds); exactly-at-floor passes (eps).
Wiring (`cycle.execute_proposals`): per-quadrant floor read from state + threaded; the vetoed
shadow entry carries `quadrant` + `id`. CP9: arm-guard uses the same effective floor (arm/fire
agree). Integration: a low-vol 1.7-RR trade vetoed at the 2.0 seed; the quadrant learns down to 1.6
over ≥8 won shadow resolutions; an equivalent trade is then admitted. Full suite green; zero
net-new ruff.

## Out of scope (YAGNI)

- Per-symbol or continuous (non-bucketed) floors — quadrant buckets are enough.
- Taken-trade realized-RR signal — shadow-veto outcomes are the agreed signal; revisit only if the
  shadow signal proves too sparse.
- Adapting `MIN_RR` itself or any other gate limit — only the RR floor, only within `[1.6, 2.5]`.

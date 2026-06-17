# Open-air-TP RR guard (protected `risk_gate`) — Design

**Status:** approved (user said "fix it" 2026-06-17, authorizing the protected `risk_gate` + `cycle` edits).

## Problem

The gate's RR floor (`risk_gate.evaluate`, via `_reward_risk`) computes reward÷risk against the
nearest **supplied** take-profit but never checks that the TP sits at real price structure. So a
proposal can **manufacture** a passing RR by placing its first TP into open air — beyond the first
real resistance/support the price must actually clear — and the deterministic gate passes it. Today
the only backstop is the Research Manager noticing each time (it correctly flatted ZEC cy100/103/104,
BNB cy101, ADA cy105 on exactly this geometry — the recurring pattern behind lesson `bd7ae076`). That
backstop is an LLM judgment call; the gate should enforce it deterministically as defense-in-depth.

## The crucial scoping refinement (discovered during design)

A naïve "veto any TP beyond structure" is **wrong** — it would veto legitimate breakouts. A breakout
long *enters at/above* the broken swing-high and legitimately targets a measured move into new
territory (SOL cy96 won exactly that way, +1.94R). A breakdown short is the mirror. So beyond-the-swing
TPs are valid **when the entry is at/beyond the swing** (the structure is behind the entry).

The defect is the **other** geometry: a **now-entry** whose first real obstacle (a swing) sits
**between the entry and the TP**. There the realistic first target is that swing; claiming RR to a TP
*past* it is the gamed RR. Concretely:

- ZEC cy104: long entry ~506.8, swingH 544.3 (the only resistance), phantom TP ~615. RR to the
  **real** resistance 544.3 is ~1.1–1.3 (< floor); the TP past 544.3 fabricated RR ≥ 2.

So the rule keys on **"is a swing strictly between entry and the nearest TP?"**, not "is the TP beyond
structure." This vetoes the ZEC-type fabrication while leaving breakouts/breakdowns (entry at/beyond
the swing → no swing between) and conservative TPs (TP short of the swing → no swing between)
**untouched**.

> Note: ADA cy105 / BNB cy101 were breakdown shorts entering *at* the swing with a measured-move TP
> beyond it — valid geometry, the mirror of a breakout. Those flats were discretionary *exhaustion /
> no-fuel* judgments, NOT geometry bugs, and correctly stay with the RM. This guard does not (and
> should not) replicate them.

## Architecture (minimal, strengthen-only)

### 1. `risk_gate.GateInputs` — PROTECTED, add two optional fields

```python
swing_high: float | None = None   # nearest structural resistance (brief swing_levels)
swing_low:  float | None = None   # nearest structural support
```

Default `None` → the guard is dormant (byte-identical to today). Backward-compatible: every existing
caller/test that omits them is unaffected.

### 2. `risk_gate._structure_capped_reward_risk(p, swing_high, swing_low) -> float | None` — PROTECTED, NEW, pure

Re-measures reward÷risk to the **first swing that lies strictly between entry and the nearest TP**:
- long: fires only when `entry < swing_high < nearest_tp` → `reward = swing_high - entry`
- short: fires only when `nearest_tp < swing_low < entry` → `reward = entry - swing_low`
- returns `None` (no cap) when no such intervening swing exists, no swing is supplied, the proposal
  has no TP, or `risk <= 0` — caller then relies on the raw RR alone.

### 3. `risk_gate.evaluate` — PROTECTED, one added veto AFTER the existing raw-RR floor

```python
rr = _reward_risk(p)
floor = max(inp.rr_floor if inp.rr_floor is not None else MIN_RR, HARD_MIN_RR)
if rr < floor - _RR_EPS:
    return veto(f"RR {rr:.2f} < min {floor:.2f}")              # unchanged
capped = _structure_capped_reward_risk(p, inp.swing_high, inp.swing_low)   # NEW guard
if capped is not None and capped < floor - _RR_EPS:
    return veto(f"open-air TP: RR-to-structure {capped:.2f} < min {floor:.2f} (TP beyond swing)")
```

**Strengthen-only invariant:** `capped <= rr` always (capping the reward to a nearer level can only
shrink it), so this can only ADD a veto, never approve a trade the raw floor rejected, never widen
risk, never weaken a limit. With `None` swings it is a no-op. This satisfies the protected-module rule
(a fix may not weaken a limit/breaker; this only tightens).

### 4. `cycle.execute_proposals` — PROTECTED, populate the swings (activates the guard)

The loop already computes `_cdf = last_completed_frame(ctx.frames[unified], now, tf)` for the RR-floor
quadrant. Reuse it: `from futures_fund.baseline import swing_levels`; `sh, sl = swing_levels(_cdf)`
(guarded for empty `_cdf` → `None, None`); pass `swing_high=sh, swing_low=sl` into `GateInputs`. This
is the live path; the gate uses the same completed-bar swing levels the briefs/RR-floor quadrant use.

## Edge cases / invariants

- No swing supplied / `None` → dormant (today's behavior).
- Breakout long (entry ≥ swing_high) / breakdown short (entry ≤ swing_low) → no swing between →
  **unaffected**. Verified against the SOL-cy96 winner shape.
- TP short of the swing (conservative target) → no swing between → raw RR stands.
- `risk_per_unit <= 0` or no TP → `None` (the raw-RR path already handles these).
- The guard uses the **same** nearest-TP selection as `_reward_risk` (consistency).

## Testing

- **`tests/test_risk_gate.py`** (pure): ZEC-type long (swingH between entry and a phantom TP →
  capped RR < floor → veto) ; mirror short ; breakout long (entry above swingH → no cap → passes) ;
  breakdown short (entry below swingL → no cap → passes) ; TP short of the swing → raw RR stands ;
  `None` swings → byte-identical to today ; `capped <= rr` strengthen-only property ; a now-entry
  whose capped RR still clears the floor → passes (no false-veto).
- **Regression:** a full `evaluate` on a ZEC-like proposal with swings now vetoes "open-air TP"; the
  same proposal with `None` swings passes (proving the guard is the only behavioral change).
- Full `uv run pytest` green; zero net-new ruff; 3-skeptic adversarial review (strengthen-only
  invariant + breakout-not-broken + the ZEC veto) before commit. Direct-to-main. Trailer:
  `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.

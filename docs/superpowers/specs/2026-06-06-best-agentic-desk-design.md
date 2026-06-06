# Best Agentic Trading Desk — Design Spec (2026-06-06)

**Goal:** make Operation TEMPEST *actively pursue 5%/month* (it sat ~flat cy33-46, ignoring the
goal), win in *all* market conditions, and keep improving — with a standing audit so there are no
bugs. Four pillars: DEPLOY, ADAPT, IMPROVE, AUDIT. Owner directives: funding-arb SKIPPED; pacing is
utilization-only (never exceed survival caps); market-neutral → all-weather.

## Review outcome (what is real vs noise)

A 6-lens adversarial workflow (35 agents) ran. Its **design diagnosis is solid** and independently
re-confirmed by reading the code:
- Risk policy is **one-directional** — `policy.circuit_breaker` only ever de-risks (dd step-down,
  loss-halts, −12% flatten); nothing scales deployment UP toward target. `monthly_target=0.05` is a
  toothless scorecard nag. **No month-to-date pacing exists.**
- Architecture is **single-leg, directional, 4h-centric**; agents run ONE playbook (positioning
  flush-short / squeeze-long) regardless of regime; no range/MR, no relative-value, no longer holds.
- "Market-neutral = ~zero net exposure" became an **excuse to stay flat** ("can't pair from zero →
  rate flat").

Its **bug list is mostly noise** — the workflow's verify pass failed to run, and on direct
inspection the top calc "bugs" are FALSE POSITIVES: (a) quadrant caps "backward" — WRONG, less risk
in the worse regime is correct; (b) funding `max(0.0,…)` clamp "biases shorts" — WRONG, it is a
symmetric *reporting* conservatism that never feeds approve/veto/sizing; (c) partial-reduce "margin
miscalibration" — WRONG, margin scales proportionally and the kept liq is conservative. **One
genuine theme survives:** the gate trusts agent-supplied entry/stop/atr without cross-checking
against brief ground truth (anti-hallucination) → folded into Pillar 4. Lesson: the standing audit's
**verify pass is mandatory** (it caught nothing today because it failed; I verified by hand).

## Protected boundary (NEVER edit; a change may not weaken a limit/breaker/safety path)

`risk_gate, executor, exits, consolidation, policy, liquidation, sizing, cycle`. ALL new logic lives
in NEW non-protected modules + agent prompts + `orchestration.py`/`scorecard.py` wiring. The gate's
caps are the survival floor; pacing raises *utilization within* them (never the cap); the
anti-hallucination layer ADDS a check (never removes one). Full `uv run pytest` green every commit.

## Pillar 1 — DEPLOY: monthly risk-pacing (new `futures_fund/pacing.py`)

`pacing_state(state_dir, now, health, exposure, *, monthly_target=0.05) -> PacingState` reads
`equity_log.equity_series` (already per-cycle) and computes:
- **MTD return** = latest equity / (last equity at/before the 1st-of-month anchor) − 1.
- **pro-rated pace** = `monthly_target * days_elapsed / days_in_month`; `pace_gap = mtd − pace`.
- **drawdown** = `health.drawdown_from_peak`; **utilization** = `open_heat / max_heat`.
- Output `mode ∈ {soft, normal, press, throttle}` + `appetite ∈ [0,1]` + a directive string:
  - **throttle** when `mtd >= monthly_target` (target hit) → ease, bank, get selective.
  - **press** when `pace_gap < −PRESS_GAP` AND `drawdown < CAUTION_DD (0.05)` AND `utilization <
    PRESS_UTIL` → deploy more.
  - **soft** early-month (`days_elapsed < SOFT_DAYS`) → conservative start, preserve budget.
  - **normal** otherwise.
  - **ANTI-MARTINGALE INVARIANT (hard):** `drawdown >= CAUTION_DD` ⇒ mode can NEVER be `press`
    (forced to ≤ normal). Pressing happens with *unused budget*, never into losses; the breakers own
    the drawdown path. This is the single most important safety property of the whole change.

Wiring (non-protected): `orchestration.preflight_step` computes it → stamps `context.json.pacing`;
`scorecard` surfaces it. It influences ONLY:
1. **Agent prompts** (Watcher/RM/Trader get `pacing.directive`): press → "behind pace, deploy — take
   every gate-clearing edge-aligned setup at full size; rate flat ONLY on a genuinely failed thesis";
   throttle → "target hit, be selective / bank".
2. **Trader `risk_mult` default**: press → 1.0 (full budget); soft → 0.5 starters. (Still clamped ≤1
   by the gate — never exceeds the cap.)
3. **RM take-it bar**: press lowers the conviction threshold for `flat` vs actionable.

Tests: pace-gap math; month-anchor; all 4 mode transitions; **the anti-martingale invariant
(behind + drawdown ⇒ never press)**; throttle at target; empty/short equity log → soft default.

## Pillar 2 — ADAPT: regime-routed playbook + broadened menu + all-weather reframe

**Regime router (agent-prompt + a `playbook.py` reference the orchestrator injects):** strategy
switches with the existing regime quadrant (`regime.py`):
- `*_trend` → trend/breakout/pullback-continuation + positioning flush-short/squeeze-long.
- `*_range` → MEAN-REVERSION: fade range extremes (short resistance / long support), RR≥2; +
  relative-value.
- `high_vol_range` (madness) → smaller size, selective, prefer relative-value / carry-ish.
- `transition` → confirmation-gated only.

**Broadened menu (agent guidance + lessons + examples; NO data-model change):**
- Range/MR setups (must clear the protected RR≥2 floor — structure ≥2:1 on wide ranges).
- Relative-value pairs = long-strong + short-weak as **two independent gate-approved single legs**
  (the book already holds both sides; no pair object needed — funding-arb explicitly skipped).
- Longer holds via existing `horizon_hours`; the HOLD/CLOSE review must respect a multi-day thesis
  (don't close a longer-horizon trade on 4h noise).

**All-weather reframe:** reword `portfolio.exposure_warning` from "MARKET-NEUTRAL mandate: prefer X
to rebalance" → a SOFT diversification signal that explicitly states a single regime-aligned
directional position is VALID and expected, and the nag asks to diversify only when a quality
other-side setup EXISTS — never to stand flat. Update CLAUDE.md, SKILL.md, agents/*.md, and memory:
market-neutral = profit in ALL conditions; net exposure = a managed risk parameter, not forced-zero.

## Pillar 3 — IMPROVE: strengthen the learning loop + measure improvement

- Audit the existing reflector→lessons→eval-harness loop actually promotes/demotes VALIDATED lessons
  on realized outcomes (verify `lessons.py` + `promote_lesson_cli` wiring is live, not skipped).
- Add an **improvement panel** to the scorecard: trailing trend of Sharpe / hit-rate / DSR /
  deployment-rate (opens per cycle), so the desk measures whether it is getting better, plus a
  "lessons-applied" signal (did the team act on retrieved lessons?).
- Month-end / every-K-cycle **meta-reflection**: did behavior match lessons? did it deploy toward
  5%? feeds the next month's pacing posture.

## Pillar 4 — AUDIT: anti-hallucination guard + standing self-audit + tests

- **Anti-hallucination cross-validation** (new `futures_fund/proposal_audit.py`, non-protected, runs
  at gate-entry in `gate_execute_step`): cross-check each proposal against brief ground truth —
  `entry` within X% of brief `last_close`/mark; `atr` within tolerance of brief `atr`; stop/TP
  geometry sane. Large divergence → veto + log (a fabricated number can't reach execution). This is
  calc-vigilance made deterministic; it ADDS a check, weakens nothing.
- **Standing self-audit**: package the multi-lens adversarial review as a repeatable script/workflow
  template WITH a working verify pass (today's failed) — runnable on demand / scheduled. Keep the
  595-test suite as the regression backstop, green on every change.

## Staged implementation (each phase: TDD, full suite green, no protected edits, commit)

- **Phase 1 — Pillar 1 pacing engine** (biggest lever: makes the desk actually trade).
- **Phase 2 — Pillar 2 adapt** (regime router + broadened menu + all-weather reframe).
- **Phase 3 — Pillar 4 audit** (anti-hallucination guard + standing self-audit capability).
- **Phase 4 — Pillar 3 improve** (improvement metrics + meta-reflection cadence).
The 4h loop keeps running on current logic between phases — nothing left unmanaged.

## Open decisions (for owner sign-off before building)
1. **Pacing window:** calendar month (matches "5%/month"; recommended) vs rolling 30d.
2. **Press aggressiveness:** default thresholds `PRESS_GAP≈1%`, `SOFT_DAYS≈5`, `PRESS_UTIL≈0.5`
   (tune after observing); confirm "press = more setups + full size within caps", not cap-raising.
3. **Sequencing:** Phase order 1→2→4→3 (deploy first). Confirm or re-order.

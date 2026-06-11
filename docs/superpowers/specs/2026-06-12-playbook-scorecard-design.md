# Playbook Scorecard (Learning Direction A) — Design Spec

**Date:** 2026-06-12
**Status:** approved (user `yes`, after a 15-agent adversarial design review → GO-WITH-MITIGATIONS)

## Goal
Give the desk a quantitative memory of **what has actually worked in each kind of trade** — an
advisory "playbook scorecard" injected into the Research Manager so the team reasons WITH its own
realized track record, the way it already reasons with the regime read and the lessons corpus. First
step of the user's **A→B→C, advisory-only** learning roadmap. Must NOT change the goal, the gate, or
any protected limit.

## Hard constraints (from the adversarial review)
- **Non-protected, read-only.** New module `futures_fund/playbook_scorecard.py` + `scripts/playbook_cli.py`.
  Reads ONLY `memory/episodic/*.jsonl` (via `read_all_decisions`) + `state/regime_history.jsonl`.
  ZERO edits to risk_gate/executor/exits/consolidation/policy/liquidation/sizing/cycle. No decision-time
  stamping (would touch protected `cycle.py`). Injection is an ORCHESTRATOR step (run the CLI, paste the
  string into the RM prompt) — exactly like the regime read / lessons today.
- **Advisory-only.** Output is a human-readable string for the RM only. No `suggested_size`/`suggested_rr`.
  No state file any protected module reads.

## The single most important correction (review §4)
On the desk's actual 17 closed trades, **shorts are 38%-hit but +$340 (the only profitable book); longs
are 44%-hit but −$249 (losing).** And the gate enforces RR≥1.6, so **winning setups are sub-50%-hit by
design.** Therefore the headline metric is **expectancy (average R-multiple), never hit-rate** — a
hit-rate-led scorecard would defame the desk's best (low-hit/high-payoff) trades and back-door a long
bias. Hit-rate is shown only as a secondary, interval-bounded figure.

## Data reality (verified)
18 decisions / 17 closed. `r_multiple`, `regime`, `setup`, `kind` are **null on 100%** of records;
`dominant_signal` is uselessly always `"research_manager"`. Reliable inputs: `direction, entry, stop,
take_profit, size, leverage, realized_pnl, fees, funding_paid, cycle, symbol`. `regime_history.jsonl`
starts at cycle 16 (covers 16–74) and is **47 risk_off / 8 risk_on / 4 mixed** — so per-regime cells are
empty today; the honest posture is **regime-pooled per setup** with visible coverage counts.

## Components (`futures_fund/playbook_scorecard.py`)
1. **`reconstruct_r(rec) -> (gross_r, net_r) | (None, None)`** — `risk = size·|entry−stop|`; `net_r =
   realized_pnl/risk`; `gross_r = (realized_pnl + fees + funding_paid)/risk`. None if risk≤0/non-finite or
   no realized_pnl. (Both reported so fee drag is visible, not a structural tilt-to-caution.)
2. **`classify_setup(rec, regime_label) -> {side, regime_alignment, archetype}`** — OUTCOME-BLIND,
   side-agnostic. `side`=direction; `regime_alignment` ∈ {with, counter, neutral} from direction × regime
   (long+risk_on / short+risk_off = with; opposites = counter; mixed/None = neutral); `archetype`=
   `"unclassified"` (no swing/kind data yet — a forward axis when setups get stamped). Takes NO outcome
   field (locked by test).
3. **`recover_regime(cycle, regime_by_cycle) -> str|None`** — fail-CLOSED join on the **news-blind
   `deterministic_regime`** key; None if no row → trade EXCLUDED from buckets, counted in `n_unjoinable`.
4. **Stats helpers** — `wilson_interval(k,n)`, `mean_r_ci(values)` (deterministic normal/t approx — NO
   RNG), `beta_binomial_shrink(k,n,a,b)` (posterior mean toward the pooled base rate). Multiplicity via
   the in-repo `futures_fund/vendor/overfit_detector.holm_correction`.
5. **`aggregate_playbook(decisions, regime_by_cycle, *, min_n=8) -> dict`** — reconstruct R, recover
   regime, classify; bucket **by side** (pooled) and **by (side,regime_alignment)** (surfaced only if the
   cell clears `min_n`). Per bucket: n, hit-rate+Wilson, gross/net mean-R+CI, shrunk hit-rate, Holm
   significance vs base rate, `inconclusive` flag (R-CI straddles 0). Coverage diagnostics: n_total,
   n_closed, n_unjoinable, n_dropped, per-regime counts.
6. **`format_playbook_advisory(agg, *, book_flat, total_closed, dormancy_n=60) -> str`** — three-tier
   cold-start (EMPTY→"no record yet, neutral"; THIN n<min_n→"keep sampling", NO numbers; ESTABLISHED→show
   expectancy-led stats). Two-sided & pro-deployment: ≥1 "working edge → take it" line whenever any bucket
   is positive-expectancy, equal prominence to any caution; NEVER only-caution. Cautions self-silence when
   the book is flat/under-deployed. Caution phrasing = "size down / demand confirmation", never
   "avoid/skip". Global dormancy: total_closed < dormancy_n → mostly abstain, say so.

## Locked-invariant tests (acceptance criteria — build is not done without them)
- **outcome-invariance**: scramble `realized_pnl` → `classify_setup` output unchanged.
- **classifier signature** takes no outcome field (introspection).
- **mirror-symmetry**: sign-flip the book (long↔short, mirror entry/stop/tp) → buckets mirror exactly;
  never one-named/one-"unknown".
- **long/short R symmetry**: symmetric inputs → symmetric R stats.
- **no-directive-below-MIN_N**: n<8 bucket emits "insufficient sample", NO hit-rate/avg-R number.
- **never-only-caution**: any positive-expectancy bucket ⇒ advisory carries a "working edge" line.
- **expectancy-not-hitrate guard**: a sub-50%-hit/positive-expectancy bucket is NEVER labeled "caution";
  a high-hit/negative-expectancy bucket is NEVER labeled "favorable".
- **fail-closed regime join**: a trade whose cycle has no regime row is EXCLUDED (counted), not bucketed
  as "unknown".
- **R reconstruction**: gross vs net correct on a worked example; risk≤0/non-finite/no-pnl → dropped.
- **self-silence-when-flat**: cautions suppressed when `book_flat`.
- **abstain on empty/corrupt/all-pending**: returns a safe "cold-starting / abstains" string, never raises.
- **protected-boundary guard (AST/grep)**: the module imports no protected module and contains no write
  ops (`open(...,'w')`, `save_*`, `append_*`); no protected module references it.

## Carry-forward to B/C
Min-n + fail-closed abstain reading as "UNKNOWN — explore" (never "avoid"); expectancy-led +
multiplicity-corrected + two-sided with cautions self-silencing when flat; forward-only non-circular
attribution in a non-protected post-hoc aggregator (never stamp protected `cycle.py`).

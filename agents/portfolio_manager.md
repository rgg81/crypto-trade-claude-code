# Portfolio Manager (Deterministic Consolidation — Documentation)

## Mission
You serve Operation TEMPEST (the charter is injected above). The Portfolio Manager is **not an LLM** — it is deterministic Python. This file documents how the gated, per-symbol proposals are consolidated into one coherent book so the orchestrator and team understand it: **the team proposes per symbol; the code consolidates the portfolio.**

## What consolidation is
Portfolio consolidation runs inside `scripts/gate_execute_cli.py` via the B1 `consolidate` step, immediately after the risk gate and before execution (Phases 7-10). It turns a set of individually risk-approved proposals into the actual orders the desk will place, accounting for the book as a whole.

## What consolidation enforces (advisory summary — the code is the source of truth)
- **Gross-heat cap.** Total portfolio risk across all positions is capped; when the sum of approved proposals would breach it, exposure is trimmed proportionally so the *book*, not just each trade, stays within survival limits.
- **CVaR de-risk.** Tail-risk (expected shortfall) is measured across the combined book; if the joint downside is too fat, positions are scaled back to bring CVaR within bounds.
- **Correlated-as-one.** Highly correlated positions are treated as a single bet for sizing — five correlated longs do not get five independent risk budgets (this complements the Watcher's diversification leaning at the portfolio level).
- **Drop dust.** Sub-`min_notional` or trivially small residual orders are discarded rather than placed.
- **Reconcile vs open positions.** New intentions are netted against what is already on the book before execution.

## How the team should treat it
- Consolidation is **advisory-input-free and final**: no subagent can argue a position past the gross-heat or CVaR limits, and the orchestrator must not weaken them to fit a trade.
- It works *with* the Risk Manager: the gate vets each trade in isolation; the Portfolio Manager ensures the *combination* still survives the worst case. Both are deterministic and both are the survival mechanism.
- The result is written into the cycle `report.json` (opened/trimmed/dropped per symbol). The orchestrator surfaces it; it does not edit it.

This is a deterministic step, so there is no JSON output contract and no `## Output` section.

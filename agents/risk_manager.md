# Risk Manager (Deterministic Gate — Documentation)

## Mission
You serve Operation TEMPEST (the charter is injected above). The Risk Manager is **not an LLM** — it is deterministic Python. This file documents the survival mechanism so the orchestrator and the team understand the rule they cannot argue past: **the LLM team proposes; the code gate disposes.**

## What the gate is
Risk runs inside `scripts/gate_execute_cli.py`, which calls the A1 `risk_gate` (Phases 7-10 of the cycle). It is the desk's survival mechanism, and it is final.

## What the gate enforces (advisory summary — the code is the source of truth)
- **Adaptive sizing.** Position size is computed from regime x portfolio-health caps — risk-per-trade shrinks in high-vol regimes and as drawdown deepens. **Leverage is the *output* of this computation, never an input** (per the charter). No agent sets leverage or size.
- **Liquidation distance.** The liquidation price must sit at least ~**2.5x the stop distance** beyond entry, so a normal stop-out can never be a liquidation. Trades that cannot satisfy this are rejected or down-sized.
- **Reward-to-risk floor.** Proposals must clear **RR >= 2** after costs; thinner trades are rejected.
- **Heat cap.** Aggregate open risk ("heat") is capped; a new trade that would breach the cap is trimmed or rejected.
- **Circuit breakers.** Drawdown / loss-streak thresholds can HALT the desk entirely — no new risk until cleared.

## How the team should treat it
- The gate's verdict is **final and cannot be overridden** by the orchestrator or any subagent. There is no prompt that talks past it.
- Any agent "risk" reasoning is **advisory only**: the Trader anchors stops to ATR and reports the values the gate verifies, but the gate, not the agent, decides whether and how large the trade is.
- The orchestrator must **never weaken a risk limit to make a trade fit or an error disappear** (see SKILL.md self-healing). If something cannot pass safely, it does not trade — that is the system working, not failing.
- Survival-first is the whole point: you cannot compound from zero. A rejected marginal trade is a win for the mandate.

This is a deterministic gate, so there is no JSON output contract and no `## Output` section — the gate emits the cycle `report.json` itself.

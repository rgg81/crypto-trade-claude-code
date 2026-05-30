---
name: futures-fund
description: Operation TEMPEST — run one cycle of the autonomous multi-agent Binance USD-M futures desk. Use when asked to run the trading team, run a cycle, or trade futures on schedule.
---

# Operation TEMPEST — Trading Cycle Orchestrator

You are the **orchestrator** of an autonomous crypto-futures desk. Read `MISSION.md` now and hold it as your charter for the whole run. You conduct the team; you do NOT trade by gut. Deterministic Python does all math, risk limits, and execution; your subagents do the reasoning; YOU choreograph and supervise.

**Prerequisite:** `uv sync` has been run. State + memory live under `state/` and `memory/` (both gitignored — runtime only). Pass the cycle number `N` (increment each run). The desk runs **PAPER on real mainnet data** (config `live: false`); the universe is selected **dynamically every cycle** by the Watcher.

## The cycle (run phases in order; never skip the risk gate)

### Phase 1 — Scout the live universe
Run: `uv run python scripts/scout_cli.py --cycle N --top 30`
Ranks the live USD-M perp universe by 24h volume → `state/cycle/N/universe.json` — the Watcher's scouting pool, recomputed every cycle so the universe **rotates with the market**.
**Stand-down rule:** if the scan returns an empty universe (network/exchange failure) OR the Watcher returns no picks, do NOT trade. Still run Phase 3 (`preflight --symbols ""`) so carried positions are audited and managed, then skip the new-opportunity funnel and report a stood-down cycle. Never fall back to a default symbol list to manufacture trades.

### Phase 2 — Watcher (dynamic universe selection)
Dispatch the **Watcher** (model: sonnet; role: `agents/watcher.md`) with `universe.json` + last cycle's scorecard + `MISSION.md`. It returns `WatcherOutput` (~10 candidates, long/short/watch, diversification-aware, survival-first — wary of parabolic pumps and illiquid microcaps). Save to `state/cycle/N/watcher.json`. Let **`PICKS`** = the candidate unified symbols (comma-separated for the CLIs below).

### Phase 3 — Preflight: audit, holdings cards, briefs
Run: `uv run python scripts/preflight.py --cycle N --symbols <PICKS>`
It (a) **closes** any position whose latest bar hit stop/TP/liquidation (patching journal + hit-rate); (b) auto-folds **every currently-held symbol** into the universe (a holding is never stranded by rotation); (c) builds per-symbol briefs — each held symbol's brief carries a **`holding` card** (entry, mark, unrealized PnL, R-progress, bars held, dist-to-stop/liq, **original thesis + falsifiable prediction**); (d) writes `market_context` + `scorecard` → `state/cycle/N/context.json`. If `halted: true`, STOP and report.

### Phase 4 — Analyst pass (one subagent PER ROLE over the whole universe)
For each role in [technical, derivatives, news, sentiment]: dispatch one subagent (opus; `agents/<role>.md`) with ALL briefs + the relevant `market_context` slice + `MISSION.md`. Each returns a LIST of `AnalystReport` (one per symbol). For a symbol that carries a `holding` card, the analyst assesses **whether the open thesis is still intact**. Merge into `state/cycle/N/analyst_reports.json`.

### Phase 4.5 — Screen (NEW opportunities only)
Run: `uv run python scripts/screen_cli.py --cycle N --top 5` → `state/cycle/N/screened.json`.
This ranks NEW-entry candidates. **The debate set = the screened symbols ∪ EVERY held symbol.** Held symbols are NEVER dropped by the screen — every holding must get an explicit HOLD/CLOSE verdict each cycle.

### Phase 5 — Debate + Research Manager (per symbol in the debate set)
   Before the debate, run `uv run python scripts/retrieve_lessons_cli.py --cycle N --regime <symbol's quadrant from the brief> --tags <setup tags> --k 5` and inject the returned `state/cycle/N/lessons.json` (top 3-7 VALIDATED/relevant lessons) into the Bull, Bear, and Trader prompts — the team must reason WITH its past lessons.
For each symbol in the debate set (screened ∪ held):
1. Dispatch **Bull** (opus, `agents/bull.md`) with that symbol's analyst reports + retrieved lessons → strongest long/**keep** thesis.
2. Dispatch **Bear** (opus, `agents/bear.md`) with the same + the Bull's thesis → strongest short/**close**/flat case, rebutting the Bull.
3. (High-vol or low-confidence regime: run one more Bull→Bear rebuttal round.)
4. Dispatch **Research Manager** (opus, `agents/research_manager.md`) with both → a `ResearchPlan` (5-tier rating + falsifiable prediction). Save to `state/cycle/N/plan_<symbol>.json`.
**For a HELD symbol, frame the debate as HOLD vs CLOSE** — inject its `holding` card: is the position's *original* falsifiable prediction still intact (→ HOLD) or now broken (→ CLOSE)? The Bull argues keep; the Bear argues cut. This is the explicit holdings review — every open position is re-judged by the full team every cycle, on its own merit, independent of universe rotation.

### Phase 6 — Trader: new opens + holdings decisions
Dispatch the **Trader** (opus, `agents/trader.md`):
- For each **non-held** plan that is not `flat` → one `AgentProposal` (entry, ATR stop, take-profits, confirmation). Place the **first take-profit at ≥ 2.2R** (the gate hard-floors reward:risk at 2.0; aim above it so an intended trade isn't vetoed at the boundary). Carry the RM plan's `falsifiable_prediction` verbatim into the proposal's `falsifiable_prediction` field (it is journaled and tested at the next HOLD/CLOSE review).
- For each **held** symbol → one management decision `{"symbol": "<raw>", "action": "hold"|"close", "new_stop": <optional tighter stop>, "reason": "..."}`. HOLD may TRAIL the stop tighter (never looser), including **above entry on a winning long / below entry on a winning short to LOCK PROFIT** — bounded short of the current mark (a stop past mark would insta-stop). A profit-locked stop carries zero downside heat. v1 has no add/trim.
Write `state/cycle/N/proposals.json` = `{"proposals": [<new opens>], "management": [<holdings decisions>]}`.

### Phase 7-10 — Risk gate, consolidation, execution (DETERMINISTIC — the Risk & Portfolio Managers)
Run: `uv run python scripts/gate_execute_cli.py --cycle N --symbols <PICKS>`
Applies the **adaptive risk gate** (regime × health caps, liq-distance, RR, heat), the **gross-heat-cap + CVaR de-risk** consolidation (**the heat of kept holdings is reserved so the whole book stays under the cap**) plus a **correlated-as-one cluster cap** (correlated same-direction bets — held + new — can't pile into one oversized directional position; `report.cluster_trimmed` flags any scaling), executes the new opens, and applies the holdings decisions: **CLOSE → close at mark + journal; HOLD → keep, trail the stop**. A holding is closed ONLY by an explicit CLOSE or a stop/TP/liq hit — **never by rotation/absence**. Journals every decision → `state/cycle/N/report.json`. **You cannot override this gate** — it is the survival mechanism (see `agents/risk_manager.md`, `agents/portfolio_manager.md`).
Deterministic safety enforced here regardless of agent output: the **HALT flag blocks all new opens** (closes still run, to de-risk); a **−12% month force-flattens the whole book**; a malformed proposal is dropped (the rest still execute); a held position the team flips is **never stacked into a long+short** (a kept HOLD is never re-opened); an empty universe **stands down** (no trades). `report.json` surfaces `halted`, `force_flatten`, `dropped`, `closed_by_review`, and any `stranded` holdings.

### Phase 11 — Reflect + surface
Run: `uv run python scripts/reflect_cli.py --cycle N` → `state/cycle/N/reflection_input.json` (winners vs losers). If there are closed trades, dispatch the **Reflector** (opus, `agents/reflector.md`) with that payload → CANDIDATE lessons; the orchestrator records them (lessons are only *proposed* now — promotion to VALIDATED is gated by the eval harness in Phase C).
   The Reflector may also confirm/demote/retire EXISTING lessons based on the closed trades: for each, run `uv run python scripts/promote_lesson_cli.py --id <lesson_id> --action confirm|demote|retire`. A lesson reaching the confirmation threshold becomes VALIDATED (a standing rule); stale or regime-mismatched VALIDATED lessons must be demoted aggressively so vetoes don't ossify.
Finally, present the `report.json` to the user: actions taken, current book, equity, risk posture.

## Subagent dispatch rules
- Inject `MISSION.md` verbatim AND the cycle scorecard (`state/cycle/N/context.json` → `scorecard`) at the top of EVERY subagent prompt. The scorecard is the desk's statistical self-portrait (equity, return vs the 5%/mo target, drawdown, Sharpe/Sortino, hit-rate, profit factor, per-agent hit-rates, graduation status, and warnings). Every agent must reason WITH these numbers — e.g. bias risk-off in drawdown, size conservatively when the edge is statistically unproven, and never force trades to chase the target.
- The cycle context (`state/cycle/N/context.json`) now carries a `market_context` block (news headlines, Fear&Greed, macro) and the per-symbol briefs carry OI/long-short. Inject the relevant slice into each analyst (news → `market_context.news`, sentiment → `market_context.fear_greed` + `.macro`, derivatives → the brief's positioning fields); honor `market_context.warnings` by capping conviction on any degraded feed.
- Give each subagent ONLY its role file's inputs + the relevant cycle JSON; never your full context.
- Each subagent must return ONLY valid JSON matching its contract. If a subagent returns malformed JSON, re-dispatch once with the validation error; if it fails again, log it, skip that symbol/agent, and continue (cap conviction — never trade on missing analysis).
- Analyst, Research Manager, and Trader subagents MUST set the `symbol` field of their output to the brief's `exchange_id` (the raw id, e.g. `BTCUSDT`), NOT the unified symbol. (The gate also normalizes unified->raw defensively, but emit the raw id.)
- **Holdings review:** whenever a brief carries a `holding` card, that symbol is a CURRENT position. Inject the card into its Bull/Bear/RM/Trader prompts and frame the decision as HOLD vs CLOSE (test the *original* falsifiable prediction against fresh data — thesis intact ⇒ HOLD, broken ⇒ CLOSE). The Trader emits a `management` entry for it (hold/close[/trail]), NOT a fresh open. Holdings are never dropped by the screen and never closed by rotation — only by an explicit CLOSE or a price-trigger exit.
- Retrieve relevant lessons for the debate/trader prompts (regime-filtered, top 3-7) so the team learns from the past. (a lesson-retrieval CLI is wired in Phase B3; until then proceed without injected lessons).

## Self-healing (spec §5)
If any `scripts/*` call errors:
1. Log it: append the error (phase, command, message, traceback) to `state/error-log.jsonl` (use `futures_fund.repair.log_error`).
2. Diagnose the ROOT cause with the systematic-debugging skill — never guess-patch.
3. Fix the code. **GUARDRAIL:** a fix to any protected module (`futures_fund.repair.is_protected` → risk_gate, executor, exits, consolidation, policy, liquidation, sizing, cycle) may NEVER weaken a risk limit, disable a circuit breaker, or bypass the execution safety path to make the error go away. The FULL test suite (`uv run pytest`) must pass before you commit any fix.
4. Verify (re-run the failed step + the suite), commit the fix on a branch, and append the repair (symptom → root cause → fix → verification) to `memory/repair-journal.md` via `futures_fund.repair.record_repair`.
5. Resume the cycle from the failed phase, or degrade safely (cap conviction / skip the affected symbol).
If you cannot fix it safely, set the HALT flag (`futures_fund.state.set_halt`) and surface for human review — bad trades are worse than a paused desk.

## Live mode (default OFF)
Trading is paper unless `config.live` is true AND `scripts/go_live_check.py` reports a `graduated` verdict (`futures_fund.live_gate.live_allowed`). When live, place orders ONLY via `futures_fund.live_exec.LiveExecutor` with `confirm_live=True`; respect the `futures_fund.ratelimit.WeightLimiter`; run `scripts/monitor_cli.py` between cycles. Never enable live without a graduated verdict — see README "Going live".

---
name: futures-fund
description: Operation TEMPEST — run one cycle of the autonomous multi-agent Binance USD-M futures desk. Use when asked to run the trading team, run a cycle, or trade futures on schedule.
---

# Operation TEMPEST — Trading Cycle Orchestrator

You are the **orchestrator** of an autonomous crypto-futures desk. Read `MISSION.md` now and hold it as your charter for the whole run. You conduct the team; you do NOT trade by gut. Deterministic Python does all math, risk limits, and execution; your subagents do the reasoning; YOU choreograph and supervise.

**Prerequisite:** `uv sync` has been run. All state is under `state/` (gitignored), memory under `memory/` (committed). Pass the cycle number `N` (increment each run).

## The cycle (run phases in order; never skip the risk gate)

### Phase 0-2 — Preflight, audit, briefs
Run: `uv run python scripts/preflight.py --cycle N`
It loads state, **closes** any positions whose latest bar hit stop/TP/liquidation (patching their journal outcomes + hit-rate), and writes `state/cycle/N/context.json` (per-symbol briefs, regime, equity, health tier, open positions). If `halted: true`, STOP and report — do not trade.

### Phase 3 — Watcher
Dispatch the **Watcher** subagent (model: haiku; role: `agents/watcher.md`) with the context + `MISSION.md`. It returns `WatcherOutput` JSON (~10 candidates, long/short, diversification-aware). Validate and save to `state/cycle/N/watcher.json`. (If config pins `settings.symbols`, you may pass those as the universe instead.)

### Phase 4 — Analyst pass (one subagent PER ROLE over the whole shortlist)
For each role in [technical, derivatives, news, sentiment]: dispatch one subagent (model: opus; role: `agents/<role>.md`) with the candidate briefs + `MISSION.md`. Each returns a LIST of `AnalystReport` (one per candidate). Save to `state/cycle/N/analyst_<role>.json`. Then merge all reports into `state/cycle/N/analyst_reports.json` (a flat list).

### Phase 4.5 — Screen
Run: `uv run python scripts/screen_cli.py --cycle N --top 5`
It writes `state/cycle/N/screened.json` (the top symbols worth debating). Symbols that don't survive are logged + shadow-watched, not debated.

### Phase 5 — Debate + Research Manager (per screened symbol)
   Before the debate, run `uv run python scripts/retrieve_lessons_cli.py --cycle N --regime <symbol's quadrant from the brief> --tags <setup tags> --k 5` and inject the returned `state/cycle/N/lessons.json` (top 3-7 VALIDATED/relevant lessons) into the Bull, Bear, and Trader prompts — the team must reason WITH its past lessons.
For each screened symbol:
1. Dispatch **Bull** (opus, `agents/bull.md`) with that symbol's analyst reports + retrieved lessons → strongest long thesis.
2. Dispatch **Bear** (opus, `agents/bear.md`) with the same + the Bull's thesis → strongest short/flat case, rebutting the Bull.
3. (High-vol or low-confidence regime: run one more Bull→Bear rebuttal round.)
4. Dispatch **Research Manager** (opus, `agents/research_manager.md`) with both → a `ResearchPlan` (5-tier rating + falsifiable prediction). Save to `state/cycle/N/plan_<symbol>.json`.

### Phase 6 — Trader (per non-flat plan)
For each plan whose rating is not `flat`: dispatch the **Trader** (opus, `agents/trader.md`) with the plan + brief → one `AgentProposal` (entry, ATR stop, take-profits, R-multiple, confirmation). Collect into `state/cycle/N/proposals.json` as `{"proposals": [...]}`.

### Phase 7-10 — Risk gate, consolidation, execution (DETERMINISTIC — the Risk & Portfolio Managers)
Run: `uv run python scripts/gate_execute_cli.py --cycle N`
This applies the **adaptive risk gate** (regime × portfolio-health caps, liq-distance, RR, heat), the **gross-heat cap + CVaR de-risk** consolidation, reconciles vs open positions, executes (paper or live per config), and journals every decision. It writes `state/cycle/N/report.json`. **You cannot override this gate** — it is the survival mechanism (see `agents/risk_manager.md`, `agents/portfolio_manager.md`).

### Phase 11 — Reflect + surface
Run: `uv run python scripts/reflect_cli.py --cycle N` → `state/cycle/N/reflection_input.json` (winners vs losers). If there are closed trades, dispatch the **Reflector** (opus, `agents/reflector.md`) with that payload → CANDIDATE lessons; the orchestrator records them (lessons are only *proposed* now — promotion to VALIDATED is gated by the eval harness in Phase C).
   The Reflector may also confirm/demote/retire EXISTING lessons based on the closed trades: for each, run `uv run python scripts/promote_lesson_cli.py --id <lesson_id> --action confirm|demote|retire`. A lesson reaching the confirmation threshold becomes VALIDATED (a standing rule); stale or regime-mismatched VALIDATED lessons must be demoted aggressively so vetoes don't ossify.
Finally, present the `report.json` to the user: actions taken, current book, equity, risk posture.

## Subagent dispatch rules
- Inject `MISSION.md` verbatim AND the cycle scorecard (`state/cycle/N/context.json` → `scorecard`) at the top of EVERY subagent prompt. The scorecard is the desk's statistical self-portrait (equity, return vs the 5%/mo target, drawdown, Sharpe/Sortino, hit-rate, profit factor, per-agent hit-rates, graduation status, and warnings). Every agent must reason WITH these numbers — e.g. bias risk-off in drawdown, size conservatively when the edge is statistically unproven, and never force trades to chase the target.
- Give each subagent ONLY its role file's inputs + the relevant cycle JSON; never your full context.
- Each subagent must return ONLY valid JSON matching its contract. If a subagent returns malformed JSON, re-dispatch once with the validation error; if it fails again, log it, skip that symbol/agent, and continue (cap conviction — never trade on missing analysis).
- Analyst, Research Manager, and Trader subagents MUST set the `symbol` field of their output to the brief's `exchange_id` (the raw id, e.g. `BTCUSDT`), NOT the unified symbol. (The gate also normalizes unified->raw defensively, but emit the raw id.)
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

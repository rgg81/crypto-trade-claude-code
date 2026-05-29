# 🌩️ Operation TEMPEST

**A self-improving, self-healing multi-agent crypto-futures trading desk — built as a Claude Code skill.**

> *We are an autonomous crypto-futures desk with one mandate: compound a real USD account at more than 5% every month — net of every fee, every funding payment, every slip — and survive every storm in between.*
> *Edge is earned, measured after costs, and proven before it is trusted. Leverage is the **output** of our risk, never the input. We get a little sharper every four hours. We trade the storm.*
> — [`MISSION.md`](MISSION.md)

A team of specialized LLM agents — a scout, four analysts, a bull-vs-bear debate, a judge, a trader, and a reflector — runs the desk on **Binance USD-M perpetual futures**. The LLM team *reasons*; deterministic, unit-tested Python owns **all** math, risk limits, and execution. It runs every 4 hours, manages a USD account, remembers and reflects on every past decision, and repairs its own code along the way.

`256 tests · ruff-clean · Python 3.11 · paper-by-default`

---

## ⚠️ Disclaimer

This is a **research / educational** project. It is **not financial advice**, makes **no guarantee** of profit, and ships with **no warranty**. Trading leveraged crypto futures can lose you **more than your deposit**. The live-execution path is implemented but has **not** been validated against a real exchange and is **disabled by default**. If you ever connect real capital, you do so entirely at your own risk — start on testnet, read [Going live](#-going-live-real-capital), and never risk money you can't afford to lose.

---

## How it works — the trading firm

Each cycle the **orchestrator** (the Claude running [`SKILL.md`](SKILL.md)) conducts the team and calls Python for everything deterministic:

```
 Phase 0–2   Preflight: load state · close stop/TP/liquidation hits · audit & reflect
             · detect market regime · build the per-symbol briefs + the desk SCORECARD
                 │
 Phase 3      WATCHER ........... scouts the market → ~10 long/short candidates (diversification-aware)
                 │
 Phase 4      4 ANALYSTS ........ Technical · Derivatives · News · Sentiment (one pass over the shortlist)
                 │
 Phase 4.5    SCREEN ............ conviction × agreement → top-N symbols worth debating
                 │
 Phase 5      BULL ⚔ BEAR ...... bounded debate → RESEARCH MANAGER judges → 5-tier directional plan
                 │
 Phase 6      TRADER ........... plan → concrete order (entry, ATR stop, take-profit, R-multiple)
                 │
 Phase 7–10   ┌───────────────────────────────────────────────────────────────────────┐
              │  RISK MANAGER  (deterministic gate — code, not persuasion)              │
              │  PORTFOLIO MANAGER  (gross-heat cap, CVaR de-risk, correlation-as-one)  │  ← the survival layer
              │  EXECUTION  (reconcile · resting reduceOnly stops/TPs · paper or live)  │
              └───────────────────────────────────────────────────────────────────────┘
                 │
 Phase 11     REFLECT .......... attribute realized PnL → lessons (gated promotion) · surface the report
```

Every agent's prompt is prefixed with the **mission** and the live **scorecard** (equity, return vs the 5%/mo target, drawdown, Sharpe/Sortino, hit-rate, per-agent hit-rates, graduation status, and warnings) — so the whole team reasons *with* the desk's measured track record, never blind.

### The team

| Agent | Role |
|---|---|
| **Watcher** | Scouts the market each round; nominates ~10 long/short candidates, diversification-aware |
| **Technical / Orderflow** | Price action, ATR, MACD/RSI/BB, regime, structure |
| **Derivatives** | Funding, open interest, long/short ratio, basis, liquidation clusters — the futures-native edge |
| **News / Catalyst** | Listings/hacks/regulatory/ETF catalysts + risk-off flags |
| **Sentiment / Macro** | Fear & Greed, social attention, DXY/yields/Fed overlay |
| **Bull** ⚔ **Bear** | Build the strongest long and short/flat theses; each must rebut the other |
| **Research Manager** | Judges the debate → a 5-tier rating + a *falsifiable* prediction |
| **Trader** | Converts the plan into an order with an ATR stop and ≥2R target |
| **Risk Manager** *(deterministic)* | The hard gate: adaptive `regime × portfolio-health` caps, liquidation-distance, RR, heat, circuit breakers — **the LLM cannot argue past it** |
| **Portfolio Manager** *(deterministic)* | Cross-symbol consolidation: gross-heat cap, CVaR de-risk, correlated-as-one |
| **Reflector** | Post-close attribution → lessons (promoted to standing rules only when statistically proven) |

## Design principles (the non-negotiables)

- **Edge is net of costs or it's fiction.** Every PnL accounts for maker/taker fees, funding, and slippage.
- **The risk gate is code, not vibes.** Position size comes from the ATR stop; **leverage is an output**, never an input. Correlated longs count as *one* bet. Liquidation is computed off **mark price** with tiered maintenance margin.
- **Survival first.** 5%/month is a *ceiling to respect*, not a quota to chase — the Risk Manager actively resists over-sizing; circuit breakers step risk down in drawdown.
- **Prove before you trust.** A lesson becomes a standing rule, and the desk goes live, only after a statistical gate (Deflated Sharpe Ratio ≥ 0.95, beats buy-&-hold net of costs). *(The vendored walk-forward / overfit toolkit is available for offline backtest validation; the live gate itself uses DSR on the paper track record.)*
- **Remember honestly.** Every decision is journaled *before* its outcome is known (two-phase, anti-hindsight) and judged after.
- **Self-improve & self-heal.** The team refines its beliefs from realized PnL; the orchestrator diagnoses, fixes, and commits its own code errors — but a "fix" may **never** weaken a risk limit (if it can't be fixed safely, it HALTs).

## Architecture

A Claude Code **skill**: the orchestrator (`SKILL.md`) dispatches Claude subagents (`agents/*.md`) for reasoning, passes their JSON outputs through a Python spine, and calls deterministic modules for all math / risk / execution. State lives in `state/` (gitignored, runtime), memory in `memory/` (git-versioned, auditable).

```
SKILL.md              orchestrator playbook (the phased cycle)
MISSION.md            the charter, injected into every agent prompt
agents/*.md           12 role files (watcher, 4 analysts, bull, bear, research_manager,
                        trader, risk_manager, portfolio_manager, reflector)
futures_fund/         the Python engine (40 modules), grouped by responsibility:
  · risk core         models · costs · liquidation · sizing · portfolio_risk · policy · risk_gate
  · data layer        config · exchange · market_data · vendors
  · state & memory    state · portfolio · journal · hitrate · memory_layout · lessons
  · the cycle         fills · exits · executor · consolidation · baseline · cycle · orchestration · cycle_io
  · agent rails       contracts · brief · screen · reflect
  · evaluation        equity_log · metrics · graduation · scorecard · shadow
  · live readiness    live_gate · orders · live_exec · ratelimit · monitor · repair
  · vendor/           regime_detection · feature_engineering · walk_forward · overfit_detector (vendored)
scripts/              CLIs the orchestrator runs (preflight, screen_cli, gate_execute_cli,
                        reflect_cli, retrieve_lessons_cli, promote_lesson_cli, monitor_cli,
                        scorecard_cli, graduation_cli, go_live_check, run_cycle, smoke_testnet)
tests/                256 offline tests (no network, no live LLM)
docs/superpowers/     the design spec + the per-phase implementation plans
```

The three analytical building blocks (`regime_detection`, `feature_engineering`, `walk_forward` / `overfit_detector`) are **vendored** so the repo is fully self-contained and reproducible.

## Quality & testing

- **256 tests, 100% offline.** Pure functions are tested with synthetic data; the exchange/data layer with captured fixtures; the orchestration with a **dry-run** that runs a full cycle on *fixture* agent outputs (no live LLM); role files are locked to their JSON contracts by a **schema-conformance** harness.
- **Built TDD, phase by phase**, each phase adversarially code-reviewed before merge — a discipline that caught several real bugs (a wrong exit-fee assertion, the DSR observation-count guard, a rate-limiter window edge) where the build agents *stopped rather than fudge a test*.
- The Binance liquidation formula was verified symbolically; fee/funding rates were checked against current Binance docs.

## Quickstart (paper)

Requires [`uv`](https://docs.astral.sh/uv/) and Python 3.11+.

```bash
uv sync                       # install deps + create the venv
uv run pytest                 # 256 tests, all offline
uv run ruff check .

# Run one deterministic (no-LLM) baseline cycle:
cp .env.example .env          # add Binance USD-M *testnet* keys (optional for public data)
uv run python scripts/run_cycle.py --cycle 1

# Run the full LLM team: invoke the futures-fund skill (follow SKILL.md) inside Claude Code.
```

Config lives in [`config.yaml`](config.yaml) (account size, symbols, models, verdict horizon, `testnet`/`live` flags); secrets come from the environment (`.env`), never the repo.

## Memory & learning

The desk keeps a git-versioned, auditable memory under `memory/`:

- **Two-phase decision journal** (`memory/episodic/journal-*.jsonl`) — each decision is written *before* its outcome, then patched with realized PnL + a lesson on close.
- **Lessons** (`memory/lessons/`) — CANDIDATE → VALIDATED (a hard rule) only on recurrence **and** statistical support; demoted aggressively so stale vetoes don't ossify. Regime-filtered, recency/importance/relevance-scored retrieval feeds the debate.
- **Per-agent hit-rates** + the **scorecard** the whole team sees each cycle.
- **Repair journal** (`memory/repair-journal.md`) — the orchestrator's own auditable record of code fixes.

## 🛟 Going live (real capital)

The desk is **paper-by-default** and **double-gated** before it can touch real money:

1. **Validate on testnet.** Put Binance USD-M **testnet** keys in `.env` (`BINANCE_KEY`/`BINANCE_SECRET`); keep `exchange.testnet: true`. Run cycles and confirm orders/fills look right (`scripts/smoke_testnet.py`).
2. **Earn graduation.** Run ≥20–30 audited paper cycles. `uv run python scripts/go_live_check.py` must report `graduation.status == "graduated"` (positive OOS Sharpe, **DSR ≥ 0.95**, beating buy-&-hold net of costs). Until then, live is refused.
3. **Enable live (explicit).** Only then set `live: true` in `config.yaml` and supply production keys. `LiveExecutor.place_book` *still* refuses unless called with `confirm_live=True`. Leverage is the gate's output; stops/TPs are always reduceOnly; margin is isolated.
4. **Schedule.** Full cycle every 4h (`cron`/scheduler → the `SKILL.md` orchestrator); the light risk monitor every ~15–30 min (`scripts/monitor_cli.py`) — it trips the **HALT** flag on a drawdown / liquidation-distance breach.

> **Not yet validated live.** The final paper↔live reconciliation (reading real fills instead of simulated ones) is the last integration step and must be exercised on testnet first.

### Kill switch
```bash
uv run python -c "from futures_fund.state import set_halt; set_halt('state', True, reason='manual kill')"
```
Halts all new trading immediately; the cycle short-circuits at preflight while the exchange's resting reduceOnly stops keep protecting open positions. Clear with `set_halt('state', False)`.

## Status & roadmap

| | |
|---|---|
| ✅ Risk core, data layer, state & memory, the paper cycle | built & tested |
| ✅ The LLM team, self-healing orchestrator, gated learning | built & tested |
| ✅ Evaluation, scorecard-to-all-agents, graduation gate | built & tested |
| ✅ Live-execution machinery (orders, executor, monitor, rate limiter) | built, **gated off** |
| ⏳ Testnet validation → a *graduated* verdict → real-capital go-live | **up to the operator** |

## Credits & inspiration

Distilled from research into multi-agent LLM trading systems — [TauricResearch/TradingAgents](https://github.com/TauricResearch/TradingAgents), FINCON, FinMem, QuantAgent — and [`rgg81/solana-storm`](https://github.com/rgg81/solana-storm), grounded in Binance USD-M futures mechanics. Built with [Claude Code](https://claude.com/claude-code) and the *superpowers* skill workflow (brainstorm → spec → plan → TDD → adversarial review).

*No license is set yet — by default this is "all rights reserved." Add a `LICENSE` file if you intend others to reuse it.*

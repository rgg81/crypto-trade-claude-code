# Design Spec — `futures-fund` (Operation TEMPEST)

- **Date:** 2026-05-29
- **Status:** Approved design (pre-plan)
- **Author:** Roberto + Claude
- **Topic:** A self-improving, multi-agent Binance USD-M futures trading team, delivered as a project-local Claude Code skill.

---

## 0. Mission (the charter)

A top-level `MISSION.md` holds the desk's charter. It is injected verbatim into the prompt of **every** subagent on **every** cycle so the whole team shares one identity and one set of prime directives.

> # OPERATION TEMPEST
> **We are an autonomous crypto-futures desk with one mandate: compound a real USD account at more than 5% every month — net of every fee, every funding payment, every slip — and survive every storm in between.**
>
> We are not gamblers. Edge is *earned*, measured after costs, and proven before it is trusted. We size for survival first and returns second, because **you cannot compound from zero**. Leverage is a tool we respect, never worship — it is the *output* of our risk, never the input.
>
> We think in bull **and** bear. Every thesis must defeat its strongest opponent before it earns a single dollar. We disagree loudly, decide cleanly, execute without ego.
>
> We remember. Every decision is written down *before* its outcome is known, and judged honestly after. Our wins teach us patience; our losses teach us faster. **We get a little sharper every four hours.**
>
> The market owes us nothing. We earn our keep one disciplined cycle at a time. *We trade the storm.*

The mission encodes the prime directives the whole system enforces: (1) net-of-cost edge, (2) survival before returns, (3) leverage as an output, (4) mandatory bull/bear adversarial test, (5) write-before-outcome honesty, (6) continuous learning.

---

## 1. Goal & success criteria

**Goal:** A Claude-native skill that runs every 4 hours, manages a USD-M futures account (paper/testnet first → live), and nets **> 5% per month after all costs** while surviving drawdowns.

**Success criteria:**
- The full cycle runs unattended end-to-end, idempotently, with no silent runs (every phase persists an artifact).
- All PnL is reported **net of fees + funding + slippage**.
- The team holds a genuine bull-vs-bear debate and produces clean, machine-extractable decisions.
- Memory + reflection demonstrably feed past decisions back into future ones.
- The orchestrator self-heals code errors and commits fixes.
- A statistical graduation gate guards the paper→live transition.
- 5%/month is treated as a **good-month ceiling, not a quota**; the Risk Manager actively resists over-sizing.

**Non-goals (YAGNI):** HFT/sub-second execution; market-making; options; spot; cross-exchange arbitrage; a GUI; a fine-tuned/trained LLM. These are explicitly out of scope for v1.

---

## 2. Locked decisions

| # | Decision | Choice |
|---|---|---|
| 1 | Execution model | **Claude-native skill**: orchestrator dispatches subagents; Python owns deterministic work |
| 2 | Capital path | **Testnet/paper first → live** after the graduation gate passes |
| 3 | Symbol universe | **Dynamic**, chosen each round by a dedicated **Watcher/Scout** agent (~10, diversification-aware) |
| 4 | Risk appetite | **Adaptive**: a policy matrix keyed on `market regime × portfolio health` |
| 5 | Direction | **Long and short** |
| 6 | Live authority | **Auto-execute + notify**, within hard caps, global HALT available |
| 7 | Cadence | **4h full tick + resting exchange stops + light risk monitor (~15–30 min)** |
| 8 | Paper account size | **$10,000** |
| 9 | Data | **Free-first** (Binance public + CCXT, Fear&Greed, CryptoPanic, FRED) |
| 10 | Margin mode | **Isolated** per position |
| 11 | Risk Manager authority | **Hard deterministic gate**; LLM personas advise, code decides |
| 12 | Memory | **File-based + git-versioned**; embeddings only if recall proves weak |
| 13 | Reflection | **Conservative & gated** by the walk-forward/DSR eval harness |
| 14 | Skill location | **Project-only** (`/home/roberto/crypto-trade-claude-code`); everything committed |
| 15 | Self-healing | Orchestrator diagnoses, fixes, verifies, and **commits** code errors |

**Default parameters (configurable):** skill name `futures-fund`; deep model = Opus (analysts/researchers/judges/reflection), quick model = Haiku (extraction/routing/light monitor); verdict horizon = 8 weeks of paper; debate = 1 round default, 2 in high-vol / low-confidence regimes.

---

## 3. The team (agent roster)

Spine: **narrow uni-modal analysts** (less hallucination) → **one accountable judge per stage** (no consensus mush) → a **deterministic risk gate between signal and execution** (the survival mechanism). The LLM does slow reasoning; deterministic code does all hard limits and time-sensitive execution.

| # | Agent | Role | Output |
|---|---|---|---|
| 0 | **Watcher / Symbol Scout** | Scan the market, nominate ~10 long/short candidates; diversification- & liquidity-aware (correlated setups counted as one) | ranked candidate list + lean + rationale |
| 1a | **Technical/Orderflow Analyst** | Price action, ATR, MACD/RSI/BB, regime read, order-book microstructure | per-symbol report |
| 1b | **Derivatives Analyst** | Funding, OI, long/short ratio, basis, liquidation clusters (the futures-native edge) | per-symbol report |
| 1c | **News/Catalyst Analyst** | CryptoPanic catalysts (listings/hacks/regulatory/ETF) + lean + risk-off flags | per-symbol report |
| 1d | **Sentiment/Macro Analyst** | Fear&Greed, social attention, FRED macro (DXY/yields/Fed) overlay | per-symbol report |
| 2 | **Bull Researcher** ⚔ **Bear Researcher** | Bounded turn-based debate, each rebutting the other, drawing on reports + retrieved lessons | debate transcript |
| 3 | **Research Manager** *(judge)* | Weigh debate → 5-tier call (Strong Long / Long / Flat / Short / Strong Short) + confidence + falsifiable prediction | directional plan |
| 4 | **Trader** | Plan → concrete order (direction, entry, ATR-stop, take-profit(s), R-multiple, intended size, funding-at-entry, horizon); requires a confirmation trigger before entry | order proposal |
| 5 | **Risk Manager** *(HARD GATE)* | Deterministic per-trade + portfolio risk; adaptive caps; fast CVaR/circuit-breaker alarm; approve/resize/**veto**. LLM risk personas (Aggressive/Neutral/Conservative) advise only | approved/resized/vetoed proposals |
| 6 | **Portfolio Manager** *(final judge)* | Cross-symbol consolidation: heat budget, correlation caps, hit-rate weighting + convergence bonus, drop dust, uniform de-lever | final sized book |
| — | **Reflector** *(async, post-close)* | Two-phase journal patch + lesson generation; low-level (read) vs high-level (action) lessons; prose→narrative agents, numeric deltas→quant/risk | journal patches + lessons |
| — | **Light Risk Monitor** *(between ticks)* | Cheap/no-debate check of drawdown, liq distance, funding, stale feeds → can trip circuit breaker / cap conviction | status + optional HALT/flatten |

Each agent has a markdown **role file** in `agents/`. A cross-symbol bus lets the BTC regime inform alt reads.

### 3.1 Dispatch granularity & funnel (cost control)

A pod-per-symbol fan-out (≈10 × 4 analysts = ~40 analyst subagents per cycle, before debate) is too token-heavy for a 24/7 loop. Instead the cycle is a **funnel** with granularity matched to stage:

- **Stages 1 (analysts):** **one subagent per analyst role**, each processing the *entire* shortlist in a single structured pass (4 analyst subagents/cycle, not 40). Output is a per-symbol row keyed by symbol.
- **Screen:** a deterministic + Research-Manager screen ranks the shortlist by combined analyst conviction/agreement and keeps only the **top N (default 3–5)** symbols for full debate. The rest are logged (and shadow-watched) but skip the expensive stage.
- **Stages 2–4 (debate → judge → trader):** **per-symbol pods, parallel**, only for the screened survivors. This is where depth matters, so the cost is spent where it pays.
- **Stages 5–6 (risk gate, portfolio manager):** run once over the full proposal set.

This keeps a full cycle at roughly 4 analyst passes + (3–5)×(debate+judge+trader) + 1 risk + 1 PM subagent invocations — bounded and tunable via the screen width `N`.

---

## 4. The cycle (phased deterministic tick)

Mirrors `solana-storm`'s discipline; every phase persists an artifact ("no silent runs"); idempotent reconciliation makes a missed/crashed cycle self-heal.

0. **Preflight** — load state (account, positions, HALT flag); check feed health; refresh account/positions from exchange. If HALT or a critical feed is down → safe-mode (cap conviction / no new entries).
1. **Audit & Reflect** — for positions closed since the last tick, fetch realized PnL net of costs; patch the journal (Phase-2 fields); update per-agent hit-rate; generate per-cycle lessons.
2. **Regime + Portfolio Health** — compute market regime (vendored `regime-detection`) for BTC and per candidate; compute portfolio health (heat, drawdown-from-peak, recent hit-rate). **This sets the adaptive risk caps for the cycle.**
3. **Watcher** — nominate ~10 symbols (long/short), diversification- & liquidity-filtered.
4. **Analyst pass + screen** — 4 analyst roles, each processing the whole shortlist in one pass (features via vendored `feature-engineering`); a screen keeps the **top N (3–5)** by conviction/agreement for full debate (rest logged + shadow-watched). See §3.1.
5. **Debate + Research Manager** — per screened symbol: bull/bear (regime-filtered lessons injected) → 5-tier plan.
6. **Trader proposals** — concrete orders per long-listed plan.
7. **Risk gate** — deterministic per-trade + portfolio risk; adaptive caps; CVaR alarm; approve/resize/veto.
8. **Portfolio Manager** — final book consolidation.
9. **Execution** — reconcile current vs target book → order deltas → place orders (paper-sim or live) + **resting stop-loss/take-profit orders on the exchange**; prefer maker/post-only; record fills + costs.
10. **Journal + surface** — write Phase-1 decision records (before outcome); write a human-readable cycle report (actions, rationale, risk posture, book, PnL); surface to user.
11. **Reschedule** — confirm next full tick (cron) + light-monitor schedule.

---

## 5. Self-healing orchestrator (the operator/mechanic loop)

Self-improvement is **two-layered**: the team improves its *trading beliefs* (memory/reflection) and the orchestrator improves the *codebase* (repairs + hardening).

The orchestrator supervises every phase and subagent. On any failure — script crash, non-zero exit, schema-validation failure, an agent explicitly reporting an error, or a data anomaly — it:

1. **Captures** the error (command, traceback, inputs, phase) to `state/error-log.jsonl`.
2. **Diagnoses** root cause using the `systematic-debugging` skill (no guess-patching).
3. **Fixes** the code (or data handling), preferring a source-level fix over a band-aid.
4. **Verifies** the fix (re-run the failed step and/or the relevant test).
5. **Commits** the fix on a branch with a descriptive message, and appends a record to `memory/repair-journal.md` (symptom → root cause → fix → verification) so repairs are auditable and learnable.
6. **Resumes** the cycle from the failed phase, or **degrades safely** (skip the affected symbol / cap conviction) if the fix is non-trivial.

**Hard guardrail:** a repair may **never** weaken a risk limit, disable a circuit breaker, or bypass the execution safety path to make an error disappear. If an error cannot be fixed safely in-cycle, the orchestrator **HALTs trading and surfaces** for human review. Changes touching `scripts/risk*`, `scripts/exec*`, or the cost model require the relevant tests to pass before the commit; if tests are missing, the orchestrator writes them as part of the fix.

---

## 6. Memory & reflection (file-based, git-versioned)

```
memory/
  episodic/journal-YYYY-MM.jsonl   # two-phase decision records (CoALA episodic store)
  semantic/beliefs.md              # evolving per-symbol / per-regime beliefs (cite journal ids)
  procedural/playbook.md           # trading rules; VALIDATED ones become hard vetoes
  lessons/lessons.md               # CANDIDATE/VALIDATED lessons + promotion state + provenance
  hitrate/agent_scores.json        # rolling per-agent hit-rate → meta-allocation weighting
  repair-journal.md                # orchestrator code-fix audit trail
state/  (gitignored)               # account.json, positions, HALT flag, caches, key references
```

**Two-phase decision journal** (defeats hindsight bias):
- *Phase 1 — at decision time (written before outcome):* `id, ts, cycle, symbol, regime, setup, direction, size, leverage, entry, stop, take_profit, r_multiple, funding_at_entry, rationale, alternatives_rejected, key_assumptions, confidence_0_1, falsifiable_prediction, dominant_signal, contributing_agents, retrieved_memory_ids`.
- *Phase 2 — on close (patch same id):* `exit_ts, realized_pnl, fees, funding_paid, slippage, prediction_correct, low_level_lesson, high_level_lesson, importance_1_10`.

Low-level lesson = "was my market read right?"; high-level lesson = "was my action/sizing/timing right?" — kept separate so corrective updates target the right component (FinAgent split).

**Retrieval:** regime-filter **first**, then `score = w_rec·0.995^hours_since + w_imp·(importance/10) + w_rel·relevance`; inject **top-K = 3–7** lessons per cycle (Reflexion bound) to keep context sharp. Tag-based + recency/importance to start; add embedding cosine only if recall proves insufficient.

**Reflection (two tiers, both gated):**
- *Light per-cycle:* a 1–3 sentence lesson appended to the journal/lessons.
- *Heavy weekly:* contrast winning vs losing trades, attribute to specific agents/setups, propose belief edits **with provenance** — but every proposed edit must pass the **walk-forward / DSR gate** before it persists (ATLAS warning: naive reflection degrades performance). Promote CANDIDATE→VALIDATED only on **recurrence + statistical support** (not 3 observations); **aggressively, regime-conditionally demote** stale VALIDATED rules so vetoes don't ossify and silently cap returns. Deliver reflection as **prose to narrative agents** but as **structured numeric deltas to quant/risk agents** (e.g., "raise RSI overbought 70→75", "cut max leverage 5x→3x").

---

## 7. Cost & risk core (deterministic Python — survival-first)

- **Fees:** maker 0.0200% / taker 0.0500% (BNB-discount toggle); round-trip taker ≈ 0.10%. Prefer maker/post-only.
- **Funding:** every 8h (00:00/08:00/16:00 UTC), charged only if held at the timestamp; project from `premiumIndex` predicted funding into expected PnL; veto adverse multi-day holds; note hourly-settlement auto-switch when funding hits the cap.
- **Slippage:** walk live L2 order-book depth to a VWAP; widen non-linearly in cascades. Round price→tickSize, qty→floor(stepSize), enforce min-notional (~5 USDT) from `exchangeInfo`.
- **Liquidation:** computed off **Mark Price** (not last/candle); **tiered MMR** with the per-tier maintenance-amount offset; re-evaluate the tier when notional crosses a bracket.
- **Sizing:** fixed-fractional from the ATR stop → `size = (equity × risk%) / |entry − stop|`; **leverage is the output**. ATR stops 1.5–3× 14-period ATR; liquidation must sit **2–3× further than the stop**. **Isolated margin** per position.
- **Portfolio heat:** sum of open per-trade risks, capped per the matrix; **treat correlated crypto longs as ONE position** (intra-crypto correlation → 1.0 in crashes).
- **Fractional Kelly ≤ ¼ Kelly**, always clamped to the per-trade cap.
- **Circuit breakers:** −3% day / −6–8% week / −10–15% month → halt new entries / force-flatten / cooldown; step-down (halve risk) at −5% from peak; global HALT flag.
- **Minimum RR 2:1** (ideally 3:1).
- All PnL is **net of fees + funding + slippage**.

### 7.1 Adaptive risk policy matrix (starter — tuned during paper)

Portfolio health: **Healthy** (dd-from-peak < 5%, weekly PnL ≥ 0) · **Caution** (dd 5–10% or losing streak) · **Stressed** (dd > 10% or weekly breaker hit).
Regime quadrants (vol × trend) from `regime-detection`: Q1 low-vol trending · Q2 high-vol trending · Q3 low-vol ranging · Q4 high-vol ranging.

| Health \ Regime | Q1 (lo-vol trend) | Q2 (hi-vol trend) | Q3 (lo-vol range) | Q4 (hi-vol chop) |
|---|---|---|---|---|
| **Healthy** | 5x / 1.5% / 10% heat | 4x / 1.0% / 8% | 3x / 1.0% / 8% (mean-rev) | 2x / 0.5% / 4% |
| **Caution** | ½ the Healthy caps across the row | | | |
| **Stressed** | **No new entries — flatten/risk-reducing trades only** | | | |

Regime *transitions* → minimum size or flat. The fast CVaR/drawdown alarm sits underneath as a hard floor regardless of cell.

---

## 8. Execution & operations

- **Reconciliation-based, idempotent** executor: compute deltas between current and target book; a missed/crashed cycle self-heals on the next run.
- Place **resting stop-loss/take-profit orders on the exchange** so protection holds between ticks even when no agent is running.
- **Paper/testnet:** `testnet.binancefuture.com`; fully-costed simulated fills. **Live (post-gate):** auto-execute + notify, behind a config flag, with the global HALT always available.
- **Ops:** shared weight-aware rate limiter across agents (~2400 weight/min); listenKey refresh (~60 min); structured error log; graceful degradation (cap conviction on stale/degraded feeds); HALT flag honored everywhere.
- **Model split:** deep model (Opus) for analysts/researchers/judges/reflection; quick model (Haiku) for extraction/routing and the light monitor — controls 24/7 token cost.
- **Scheduling:** system cron every 4h invokes the skill; a lighter cron (~15–30 min) runs the Light Risk Monitor. (ScheduleWakeup self-chaining is an alternative.)

---

## 9. Evaluation & graduation gate (composes vendored skills)

- Paper/backtest engine logs every decision; **PnL attributed per agent + conviction tier**; **shadow-watch** tracks vetoed trades at zero capital to measure the risk filter's value.
- **Graduation to live requires:** ≥ 20–30 audited cycles **and** positive OOS Sharpe, **DSR > 0.95**, **PBO < 0.5** (vendored `walk-forward-validation`), beating a buy-&-hold baseline net of costs, regime-stratified.
- Uses vendored `feature-engineering` (analyst features), `regime-detection` (the regime axis + matrix input), `walk-forward-validation` (the gate + reflection gating).
- **Pre-committed verdict horizon** (config; default 8 weeks of paper): if no DSR-validated edge net of all costs by then → retire or redesign, not indefinite bleeding.

---

## 10. Data sources (free-first)

- **Binance public Futures REST via CCXT** — klines, `fapi/v1/fundingRate`, `premiumIndex`, `/futures/data/*` (openInterestHist, global/top long-short ratios, taker buy/sell). **Self-archive `/futures/data/*` on every poll** (only ~30 days + current symbols exposed) to build backtest history.
- **alternative.me Fear & Greed** (daily) — contrarian regime overlay.
- **CryptoPanic** (free dev token) — news/catalysts.
- **FRED** (free key) — DXY proxy (DTWEXBGS), DGS10, FEDFUNDS, CPIAUCSL; de-risk around FOMC/CPI.
- **Cadence discipline:** 4h klines/OI/funding drive the loop; Fear&Greed + FRED update ~daily (cache, re-trigger downstream only on change); dedupe news by URL/title. (Optional later paid upgrades: CoinGlass liquidation heatmaps, LunarCrush social.)

---

## 11. Repo layout (project-only, all committed)

```
crypto-trade-claude-code/
  SKILL.md                # orchestrator playbook (the phased cycle + self-healing protocol)
  MISSION.md              # the charter, injected into every subagent prompt
  agents/                 # one role file per agent (watcher, technical, derivatives, news,
                          #   sentiment, bull, bear, research_manager, trader, risk_manager,
                          #   portfolio_manager, reflector)
  references/             # methodology: cost model, risk policy matrix, memory schema,
                          #   debate protocol, eval/graduation gate, ops runbook
  scripts/                # python (uv): exchange client, data vendors, indicators/regime/features
                          #   (vendored), risk math, cost model, execution/reconciliation,
                          #   memory_io, backtest/eval, light monitor, cli
  memory/                 # episodic / semantic / procedural / lessons / hitrate / repair-journal
  state/                  # gitignored runtime state (account, positions, HALT, caches, key refs)
  tests/                  # pytest: lookahead/leakage asserts, cost-model, risk-gate, reconciliation
  config.yaml             # account size, caps, symbol count, models, verdict horizon
  .env.example            # data + exchange key placeholders (real .env gitignored)
```

The three existing analytical skills (`feature-engineering`, `regime-detection`, `walk-forward-validation`) are **vendored** (relevant scripts copied + versioned here) so the repo is self-contained and reproducible.

---

## 12. Build phases (for the implementation plan)

- **Phase A — Deterministic core & plumbing (no LLM):** repo scaffolding, config, exchange client (testnet via CCXT), data vendors + self-archiving, vendored indicators/regime/features, cost model, risk math + adaptive matrix, journal/memory I/O, paper-sim executor (reconciliation + resting stops), ops guardrails (HALT, rate limiter, error log), tests incl. lookahead/leakage. Prove the whole cycle end-to-end with a trivial baseline (always-flat / simple momentum).
- **Phase B — Agents & full cycle:** `MISSION.md`, `SKILL.md` orchestrator, all role files, subagent dispatch, bounded debate protocol, judges, Reflector, memory retrieval, self-healing loop. Run full paper cycles.
- **Phase C — Eval & graduation gate:** backtest/walk-forward harness, PnL attribution, shadow-watch, DSR/PBO gate, regime-stratified baselines, verdict horizon.
- **Phase D — Live readiness:** live execution path behind a flag (auto-execute + notify), rate limiter/listenKey hardening, kill-switch drills, then graduate per the gate.

---

## 13. Top risks & mitigations

| Risk | Mitigation |
|---|---|
| Costs silently eat the edge | Every PnL/backtest net of fees + funding + slippage; prefer maker; project funding pre-trade |
| Chasing 80%/yr forces over-leverage | 5% is a ceiling not a quota; Risk Manager resists sizing up; adaptive caps |
| Liquidation/price-type confusion | Liq off Mark Price; tiered MMR + maintenance offset; re-check on bracket cross |
| Fake diversification | Correlated longs counted as one position; correlation caps in the PM |
| Overfitting / selection bias | DSR + PBO; record true trial count; deflate; walk-forward only |
| Lookahead leaks | Strict T+1 execution; point-in-time data; tests assert no lookahead |
| LLM pretraining leakage | Leakage-safe windows; relative/anonymized inputs where feasible |
| Naive reflection overfits to noise | Gate every belief change through the eval harness; numeric deltas to quant agents |
| Stale VALIDATED vetoes ossify | Aggressive, regime-conditioned demotion |
| Operational blindness | HALT flag, no-silent-runs auditing, conviction-capping on degraded feeds, listenKey/rate-limit handling |
| Bear-market fragility | Hard risk limits + ability to go flat/short — not verbal reflection |
| Self-healing weakens safety | Repairs may never weaken risk/execution limits; unsafe-to-fix → HALT + surface |

---

## 14. Open parameters to confirm during planning

These have sensible defaults (Section 2) and can be finalized in the plan: exact debate-round schedule by regime; precise matrix cell values (tuned in paper); top-K memory size; weekly-reflection day; cron command form vs ScheduleWakeup; notification channel for "auto-execute + notify".

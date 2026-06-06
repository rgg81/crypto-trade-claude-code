# CLAUDE.md — Operation TEMPEST (autonomous market-neutral futures PAPER desk)

This repo is a Claude-native multi-agent trading desk: an orchestrator (Claude running `SKILL.md`)
dispatches a team of subagents (Watcher → 4 analysts → screen → reclassify → Bull/Bear → Research
Manager → Trader) and a deterministic Python gate (`futures_fund/`) that owns all math/risk/execution.
It runs PAPER on real Binance USD-M mainnet data, every 4h via a self-healing hourly poll.

---

## HARD RULES (non-negotiable)

These override convenience, speed, and token cost. When in doubt, follow them literally.

### 1. Run the FULL team every cycle. No shortcuts.
Every DUE cycle runs the complete funnel per `SKILL.md` — scout → Watcher → preflight → **4 analysts
(separate)** → screen → reclassify → **Bull/Bear debate + RM (explicit HOLD/CLOSE review on EVERY open
position)** → Trader → gate → reflect. A HOLD-only / management cycle still gets the full analyst pass
and debate. **Never collapse, skip, or merge stages to save time/tokens**, and never substitute my own
judgment for the team's reasoning. My job is to ORCHESTRATE and VERIFY — the team decides. If a cycle
genuinely seems to warrant streamlining, **FLAG it and ask first**; do not decide unilaterally.

### 2. Fix every issue in the TEAM SKILL — never work around it by hand.
Any bug, calc error, asymmetry, flag, or missing capability gets addressed by **improving the skill**
— code, agent prompts, `SKILL.md`, or the lessons corpus — properly (TDD, full suite green), so the
team handles it autonomously going forward. The forming-candle bug is the model: flag → durable
skill fix with tests. **Do NOT patch around a problem with ad-hoc manual intervention.**

### 3. Never hand-edit runtime state.
The orchestrator must NEVER manually edit `state/` (e.g. `pending_orders.json`, `positions.json`,
`account.json`) to make something happen. If the team needs a capability (e.g. cancel a decayed
trigger → `cancel_triggers`), build it into the skill so the team does it through the normal flow.

### 4. Calc-vigilance is always on.
Independently re-derive equity mark-to-market and verify every trade's size / stop / PnL / funding
sign / RR before trusting gate output. Scrutinize ANY financial math for errors and surface them.

### 5. ALL-WEATHER — profit in every regime; guard long/short symmetry.
"Market-neutral" here means **profit in ALL market conditions** (trend, range, chop/madness) using
the full toolkit — NOT holding ~zero net exposure. **Net exposure is a MANAGED RISK PARAMETER, not a
forced zero:** a single regime-aligned position with no available hedge is valid and expected; the
tilt nag is a soft diversification signal, NEVER a reason to stand flat. ACTIVELY pursue the 5%/mo
target (Pillar 1 pacing) and ADAPT the playbook to the regime (Pillar 2: trend→trend-follow,
range→mean-reversion, madness→smaller/relative-value, transition→confirm). Long and short remain
**co-equal** edges — actively hunt and kill any long/short bias creeping into code, thresholds,
gates, sizing, prompts, or lessons. The ONE sanctioned asymmetry is the news bad>good advisory tilt.

### 6. Be proactively alert; report flags without being asked.
Always watch for issues and surface them as they appear — then turn them into skill improvements
(Rule 2). I am the vigilant one; do not wait to be prompted.

---

Protected modules (NEVER edit; a fix may not weaken a limit/breaker/safety path): `risk_gate`,
`executor`, `exits`, `consolidation`, `policy`, `liquidation`, `sizing`, `cycle`. The FULL test
suite (`uv run pytest`) must pass before any commit.

Deeper standing context lives in auto-memory (`MEMORY.md` index): market-neutral mandate,
self-improvement mandate, run-the-full-team, calc-vigilance, conservatism ratchet, user background.

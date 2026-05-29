# Futures-Fund Phase B2 — The Agent Team Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the LLM team real and runnable: the `SKILL.md` orchestrator playbook + `agents/*.md` role files (Watcher, 4 analysts, Bull/Bear, Research Manager, Trader, Reflector), plus the Python orchestration spine — a per-cycle JSON workspace, step functions/CLIs the orchestrator calls, a schema-conformance harness, and a **dry-run** that runs the full orchestration end-to-end with fixture agent outputs (no live LLM).

**Architecture:** At runtime the orchestrator (the Claude running `SKILL.md`) executes the phased cycle: it calls `scripts/*` CLIs (thin wrappers over `orchestration.py`, which uses the B1 rails) for all deterministic work, and dispatches Claude **subagents** (the `agents/*.md` roles) for reasoning, capturing each subagent's JSON output to `state/cycle/<n>/` after validating it against a B1 contract. The deterministic **Risk Manager** (A1 `risk_gate`) and **Portfolio Manager** (B1 `consolidate`) are Python — the LLM team proposes, the code gate disposes (spec §3, §3.1).

**Tech Stack:** Python 3.11 / uv, pydantic v2, pytest, ruff (the spine); markdown (the orchestrator + role files). No network/LLM in tests — agent outputs are fixtures.

**Reference:** spec §0 (mission), §3 (roster), §3.1 (funnel), §4 (cycle), §6 (memory). Builds on B1 (`contracts`, `lessons`, `brief`, `screen`, `reflect`, `cycle.execute_proposals/fetch_context/audit_and_reflect`). `MISSION.md` already exists (created in A1).

**Design decisions (within the approved architecture):**
- **Data bus:** each subagent's JSON output is saved to `state/cycle/<n>/<name>.json` after contract validation; CLIs read/write there.
- **Funnel (§3.1):** Watcher → ~10 candidates; **one analyst subagent per role** over the whole shortlist (4 subagents, not 40); screen → top-N (default 5); per survivor: Bull → Bear (rebutting) → Research Manager (5-tier plan); Trader → AgentProposal; deterministic gate+consolidate+execute; Reflector on closed trades.
- **Models:** deep=Opus (analysts, researchers, Research Manager, Trader, Reflector), quick=Haiku (Watcher screen extraction). SKILL.md sets these via the Agent tool `model:` override.
- **Debate:** 1 round default (Bull, then Bear rebutting Bull); SKILL.md notes raising to 2 in high-vol/low-confidence regimes.

---

## File Structure

```
futures_fund/
  cycle_io.py        # per-cycle JSON workspace: paths, save/load, validate-against-contract
  orchestration.py   # preflight / screen_step / gate_execute_step / reflect_step (use B1 rails)
scripts/
  preflight.py · screen_cli.py · gate_execute_cli.py · reflect_cli.py   # thin argparse wrappers
SKILL.md             # the orchestrator playbook (phased cycle choreography)
agents/
  watcher.md technical.md derivatives.md news.md sentiment.md
  bull.md bear.md research_manager.md trader.md risk_manager.md portfolio_manager.md reflector.md
tests/
  fixtures/agent_examples/*.json   # one example output per agent (conformance)
  test_cycle_io.py · test_orchestration.py · test_dry_run.py · test_agent_conformance.py
  test_role_files.py
```

---

## Task 1: Per-cycle JSON workspace (`cycle_io`)

**Files:** create `futures_fund/cycle_io.py`, `tests/test_cycle_io.py`.

- [ ] **Step 1: Write the failing test** — `tests/test_cycle_io.py`:

```python
import pytest
from pydantic import ValidationError

from futures_fund.contracts import AgentProposal
from futures_fund.cycle_io import cycle_dir, load_output, save_output, validate_output


def test_save_and_load_roundtrip(tmp_path):
    save_output(tmp_path, 3, "watcher", {"candidates": []})
    assert cycle_dir(tmp_path, 3).name == "3"
    assert load_output(tmp_path, 3, "watcher") == {"candidates": []}


def test_save_accepts_pydantic_model(tmp_path):
    ap = AgentProposal(symbol="BTCUSDT", direction="long", entry=100.0, stop=95.0,
                       take_profits=[110.0], atr=2.0, confidence=0.6)
    save_output(tmp_path, 1, "trader_BTCUSDT", ap)
    assert load_output(tmp_path, 1, "trader_BTCUSDT")["symbol"] == "BTCUSDT"


def test_validate_output_returns_model_on_good_data():
    data = {"symbol": "BTCUSDT", "direction": "long", "entry": 100.0, "stop": 95.0,
            "take_profits": [110.0], "atr": 2.0, "confidence": 0.6}
    model = validate_output(data, AgentProposal)
    assert isinstance(model, AgentProposal) and model.symbol == "BTCUSDT"


def test_validate_output_raises_clear_error_on_bad_data():
    with pytest.raises(ValidationError):
        validate_output({"symbol": "BTCUSDT", "direction": "sideways"}, AgentProposal)


def test_load_missing_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_output(tmp_path, 9, "nope")
```

- [ ] **Step 2: Run** `uv run pytest tests/test_cycle_io.py -v` — expect FAIL.

- [ ] **Step 3: Implement** `futures_fund/cycle_io.py`:

```python
from __future__ import annotations

import json
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

M = TypeVar("M", bound=BaseModel)


def cycle_dir(state_dir, cycle_no: int) -> Path:
    return Path(state_dir) / "cycle" / str(cycle_no)


def save_output(state_dir, cycle_no: int, name: str, data: dict | BaseModel) -> Path:
    """Persist an agent's output JSON under state/cycle/<n>/<name>.json."""
    d = cycle_dir(state_dir, cycle_no)
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{name}.json"
    if isinstance(data, BaseModel):
        p.write_text(data.model_dump_json(indent=2))
    else:
        p.write_text(json.dumps(data, indent=2, default=str))
    return p


def load_output(state_dir, cycle_no: int, name: str) -> dict:
    p = cycle_dir(state_dir, cycle_no) / f"{name}.json"
    if not p.exists():
        raise FileNotFoundError(f"no cycle output: {p}")
    return json.loads(p.read_text())


def validate_output(data: dict, model: type[M]) -> M:
    """Validate a raw agent output dict against its contract; raises ValidationError if malformed."""
    return model.model_validate(data)
```

- [ ] **Step 4: Run** `uv run pytest tests/test_cycle_io.py -v` — expect PASS (5 passed). Then `uv run ruff check futures_fund/cycle_io.py tests/test_cycle_io.py`.

- [ ] **Step 5: Commit**

```bash
git add futures_fund/cycle_io.py tests/test_cycle_io.py
git commit -m "feat: per-cycle JSON workspace (save/load/validate agent outputs)"
```

---

## Task 2: Orchestration step functions

**Files:** create `futures_fund/orchestration.py`, `tests/test_orchestration.py`.

These are the deterministic steps the orchestrator invokes (via the Task-3 CLIs). Each uses the B1 rails.

- [ ] **Step 1: Write the failing test** — `tests/test_orchestration.py`:

```python
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from futures_fund.config import Settings
from futures_fund.contracts import AgentProposal
from futures_fund.orchestration import gate_execute_step, preflight_step, reflect_step, screen_step
from futures_fund.state import AccountState, load_positions, save_account

UTC = timezone.utc


class FakeExchange:
    def __init__(self, frames):
        self.frames = frames

    def symbol_spec(self, symbol):
        from futures_fund.models import MmrBracket, SymbolSpec
        return SymbolSpec(symbol="BTCUSDT", tick_size=0.01, step_size=0.001, min_notional=5.0,
                          mmr_brackets=[MmrBracket(notional_floor=0, notional_cap=1_000_000,
                                                   mmr=0.004, maint_amount=0.0, max_leverage=125)])

    def ohlcv(self, symbol, timeframe="4h", limit=500):
        return self.frames[symbol]

    def funding(self, symbol):
        from futures_fund.market_data import FundingInfo
        return FundingInfo(symbol=symbol, current_rate=0.0001,
                           next_funding_ts=datetime(2026, 1, 1, tzinfo=UTC), interval_hours=8.0,
                           mark_price=float(self.frames[symbol]["close"].iloc[-1]),
                           index_price=float(self.frames[symbol]["close"].iloc[-1]))


def _uptrend(n=60):
    rng = np.random.default_rng(7)
    close = 100.0 + 0.8 * np.arange(n) + rng.normal(0, 0.05, n)
    return pd.DataFrame({
        "timestamp": pd.date_range("2026-01-01", periods=n, freq="4h", tz="UTC"),
        "open": close, "high": close + 0.2, "low": close - 0.2, "close": close, "volume": 1.0,
    })


def _settings():
    return Settings(account_size_usdt=10_000.0, symbols=["BTC/USDT:USDT"], timeframe="4h")


def test_preflight_emits_context_with_briefs(tmp_path):
    ex = FakeExchange({"BTC/USDT:USDT": _uptrend()})
    ctx = preflight_step(ex, _settings(), tmp_path / "s", tmp_path / "m",
                         now=datetime(2026, 3, 1, tzinfo=UTC), cycle_no=1)
    assert ctx["cycle"] == 1
    assert ctx["halted"] is False
    assert "BTC/USDT:USDT" in {b["symbol"] for b in ctx["briefs"]}
    assert ctx["briefs"][0]["regime"]  # brief carries the regime
    assert "equity" in ctx and ctx["equity"] > 0


def test_screen_step_returns_top_symbols(tmp_path):
    reports = [
        {"agent": "technical", "symbol": "BTCUSDT", "stance": "bullish", "confidence": 0.9},
        {"agent": "derivatives", "symbol": "BTCUSDT", "stance": "bullish", "confidence": 0.8},
        {"agent": "technical", "symbol": "ETHUSDT", "stance": "neutral", "confidence": 0.5},
    ]
    top = screen_step(reports, top_n=5)
    assert top == ["BTCUSDT"]


def test_gate_execute_step_opens_from_agent_proposals(tmp_path):
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    ex = FakeExchange({"BTC/USDT:USDT": _uptrend()})
    pf = preflight_step(ex, _settings(), state_dir, memory_dir,
                        now=datetime(2026, 3, 1, tzinfo=UTC), cycle_no=1)
    last = pf["briefs"][0]["last_close"]
    proposals = [AgentProposal(symbol="BTCUSDT", direction="long", entry=last,
                               stop=last - 4.0, take_profits=[last + 8.0], atr=2.0,
                               confidence=0.7, rationale="bull thesis won the debate").model_dump()]
    report = gate_execute_step(ex, _settings(), state_dir, memory_dir,
                               now=datetime(2026, 3, 1, tzinfo=UTC), cycle_no=1,
                               proposals=proposals)
    assert report["opened"] == 1
    pos = load_positions(state_dir)
    assert len(pos) == 1 and pos[0].decision_id is not None


def test_reflect_step_splits_winners_losers(tmp_path):
    from futures_fund.journal import append_decision, patch_outcome
    from futures_fund.memory_layout import ensure_memory_layout
    memory_dir = tmp_path / "m"
    ensure_memory_layout(memory_dir)
    did = append_decision(memory_dir, {"ts": datetime(2026, 5, 1, tzinfo=UTC), "cycle": 1,
                                       "symbol": "BTCUSDT", "direction": "long",
                                       "entry": 100.0, "stop": 95.0})
    patch_outcome(memory_dir, did, {"realized_pnl": 42.0, "prediction_correct": True})
    payload = reflect_step(memory_dir)
    assert payload["n_closed"] == 1 and len(payload["winners"]) == 1
```

- [ ] **Step 2: Run** `uv run pytest tests/test_orchestration.py -v` — expect FAIL.

- [ ] **Step 3: Implement** `futures_fund/orchestration.py`:

```python
from __future__ import annotations

from datetime import datetime

from futures_fund.brief import build_symbol_brief
from futures_fund.config import Settings
from futures_fund.contracts import to_trade_proposal
from futures_fund.cycle import audit_and_reflect, execute_proposals, fetch_context
from futures_fund.memory_layout import ensure_memory_layout
from futures_fund.portfolio import portfolio_health
from futures_fund.hitrate import hit_rate
from futures_fund.reflect import reflection_payload
from futures_fund.screen import screen_reports
from futures_fund.state import is_halted, load_account, load_positions, save_account, save_positions

_AGENT_KEY = "team"


def preflight_step(exchange, settings: Settings, state_dir, memory_dir,
                   now: datetime, cycle_no: int) -> dict:
    """Phase 0-2: load state, audit exits, build the per-symbol briefs + health/regime the
    Watcher and analysts need. Returns a JSON-serializable context dict."""
    ensure_memory_layout(memory_dir)
    account = load_account(state_dir, settings.account_size_usdt)
    positions = load_positions(state_dir)
    if is_halted(state_dir):
        return {"cycle": cycle_no, "halted": True, "briefs": [], "equity": account.balance,
                "open_positions": []}
    report = {"cycle": cycle_no, "halted": False, "opened": 0, "closed": 0,
              "carried": 0, "stuck_close": 0, "equity": account.balance, "actions": []}
    ctx = fetch_context(exchange, settings)
    positions = audit_and_reflect(ctx, positions, account, memory_dir, now, report,
                                  agent_key=_AGENT_KEY)
    save_account(state_dir, account)
    save_positions(state_dir, positions)
    health = portfolio_health(account.balance, account.peak_equity, positions, ctx.prices,
                              recent_hit_rate=hit_rate(memory_dir, _AGENT_KEY))
    briefs = [build_symbol_brief(exchange, s, settings.timeframe) for s in settings.symbols]
    return {
        "cycle": cycle_no, "halted": False, "equity": health.equity,
        "drawdown_from_peak": health.drawdown_from_peak, "health_tier": health.tier,
        "briefs": briefs,
        "open_positions": [{"symbol": p.symbol, "direction": p.direction, "qty": p.qty,
                            "entry": p.entry} for p in positions],
        "audit": {"closed": report["closed"], "carried": report["carried"]},
    }


def screen_step(reports: list[dict], top_n: int = 5) -> list[str]:
    """Phase 4.5: aggregate analyst reports (raw dicts) -> top-N symbols for debate."""
    from futures_fund.contracts import AnalystReport
    parsed = [AnalystReport.model_validate(r) for r in reports]
    return screen_reports(parsed, top_n)


def gate_execute_step(exchange, settings: Settings, state_dir, memory_dir,
                      now: datetime, cycle_no: int, proposals: list[dict]) -> dict:
    """Phases 7-10: convert agent proposals -> TradeProposals, run the A1 gate + A3b execution
    via execute_proposals (the deterministic Risk Manager + Portfolio Manager), persist."""
    from futures_fund.contracts import AgentProposal
    account = load_account(state_dir, settings.account_size_usdt)
    positions = load_positions(state_dir)
    ctx = fetch_context(exchange, settings)
    aps = [AgentProposal.model_validate(p) for p in proposals]
    trade_props = []
    rationale_by_symbol = {}
    for ap in aps:
        unified = ctx.raw_to_unified.get(ap.symbol)
        funding = ctx.fundings[unified].current_rate if unified else 0.0
        trade_props.append(to_trade_proposal(ap, funding))
        rationale_by_symbol[ap.symbol] = ap.rationale
    report = execute_proposals(ctx, trade_props, contributing_agents=["research_manager", "trader"],
                               positions=positions, account=account, state_dir=state_dir,
                               memory_dir=memory_dir, now=now, cycle_no=cycle_no,
                               agent_key=_AGENT_KEY)
    return report


def reflect_step(memory_dir) -> dict:
    """Reflection: hand the Reflector subagent the winners/losers to contrast."""
    return reflection_payload(memory_dir)
```

- [ ] **Step 4: Run** `uv run pytest tests/test_orchestration.py -v` — expect PASS (5 passed). Then `uv run ruff check futures_fund/orchestration.py tests/test_orchestration.py` — fix style (import order; the local imports inside functions are intentional to keep the module's import graph shallow — leave them, or hoist to top if ruff prefers, without changing behavior).

- [ ] **Step 5: Commit**

```bash
git add futures_fund/orchestration.py tests/test_orchestration.py
git commit -m "feat: orchestration step functions (preflight/screen/gate-execute/reflect)"
```

---

## Task 3: Orchestration CLIs + dry-run end-to-end

**Files:** create `scripts/preflight.py`, `scripts/screen_cli.py`, `scripts/gate_execute_cli.py`, `scripts/reflect_cli.py`; create `tests/test_dry_run.py`.

The CLIs are thin argparse wrappers the orchestrator shells out to. The dry-run test proves the whole orchestration plumbing runs end-to-end with FIXTURE agent outputs (no LLM).

- [ ] **Step 1: Write the failing test** — `tests/test_dry_run.py`:

```python
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from futures_fund.config import Settings
from futures_fund.cycle_io import load_output, save_output
from futures_fund.orchestration import gate_execute_step, preflight_step, screen_step
from futures_fund.state import load_positions

UTC = timezone.utc


class FakeExchange:
    def __init__(self, frames):
        self.frames = frames

    def symbol_spec(self, symbol):
        from futures_fund.models import MmrBracket, SymbolSpec
        return SymbolSpec(symbol="BTCUSDT", tick_size=0.01, step_size=0.001, min_notional=5.0,
                          mmr_brackets=[MmrBracket(notional_floor=0, notional_cap=1_000_000,
                                                   mmr=0.004, maint_amount=0.0, max_leverage=125)])

    def ohlcv(self, symbol, timeframe="4h", limit=500):
        return self.frames[symbol]

    def funding(self, symbol):
        from futures_fund.market_data import FundingInfo
        return FundingInfo(symbol=symbol, current_rate=0.0001,
                           next_funding_ts=datetime(2026, 1, 1, tzinfo=UTC), interval_hours=8.0,
                           mark_price=float(self.frames[symbol]["close"].iloc[-1]),
                           index_price=float(self.frames[symbol]["close"].iloc[-1]))


def _uptrend(n=60):
    rng = np.random.default_rng(11)
    close = 100.0 + 0.8 * np.arange(n) + rng.normal(0, 0.05, n)
    return pd.DataFrame({
        "timestamp": pd.date_range("2026-01-01", periods=n, freq="4h", tz="UTC"),
        "open": close, "high": close + 0.2, "low": close - 0.2, "close": close, "volume": 1.0,
    })


def test_full_orchestration_dry_run_with_fixture_agents(tmp_path):
    """Simulate the orchestrator: preflight -> (fixture analyst reports) -> screen ->
    (fixture trader proposal) -> gate/execute. Proves the plumbing with no live LLM."""
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    ex = FakeExchange({"BTC/USDT:USDT": _uptrend()})
    settings = Settings(account_size_usdt=10_000.0, symbols=["BTC/USDT:USDT"], timeframe="4h")
    now = datetime(2026, 3, 1, tzinfo=UTC)

    # Phase 0-2: preflight produces briefs/context
    pf = preflight_step(ex, settings, state_dir, memory_dir, now=now, cycle_no=1)
    save_output(state_dir, 1, "context", pf)
    last = pf["briefs"][0]["last_close"]

    # Phase 4: orchestrator would dispatch analyst subagents; here we stand in fixture reports
    reports = [{"agent": a, "symbol": "BTCUSDT", "stance": "bullish", "confidence": 0.8}
               for a in ("technical", "derivatives", "news", "sentiment")]
    save_output(state_dir, 1, "analyst_reports", reports)

    # Phase 4.5: screen
    screened = screen_step(reports, top_n=5)
    save_output(state_dir, 1, "screened", {"symbols": screened})
    assert screened == ["BTCUSDT"]

    # Phases 5-6: orchestrator dispatches Bull/Bear/RM/Trader; fixture trader proposal
    proposal = {"symbol": "BTCUSDT", "direction": "long", "entry": last, "stop": last - 4.0,
                "take_profits": [last + 8.0], "atr": 2.0, "confidence": 0.7,
                "rationale": "uptrend + funding tailwind; bear's mean-reversion case rejected"}
    save_output(state_dir, 1, "proposals", {"proposals": [proposal]})

    # Phases 7-10: gate + execute
    proposals = load_output(state_dir, 1, "proposals")["proposals"]
    report = gate_execute_step(ex, settings, state_dir, memory_dir, now=now, cycle_no=1,
                               proposals=proposals)
    assert report["opened"] == 1
    assert len(load_positions(state_dir)) == 1
    # the per-cycle workspace persisted each stage (no silent runs)
    assert load_output(state_dir, 1, "context")["cycle"] == 1
    assert load_output(state_dir, 1, "screened")["symbols"] == ["BTCUSDT"]
```

- [ ] **Step 2: Run** `uv run pytest tests/test_dry_run.py -v` — expect FAIL (imports exist from Task 1-2, so this should actually pass once those are in; if it fails, fix the orchestration wiring, not the assertions).

- [ ] **Step 3: Create the CLIs.** Each is a thin wrapper. `scripts/preflight.py`:

```python
"""Phase 0-2 CLI: emit the per-cycle context (briefs/health) for the Watcher + analysts.

    uv run python scripts/preflight.py --cycle N
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

from futures_fund.config import load_settings
from futures_fund.cycle_io import save_output
from futures_fund.exchange import FuturesExchange
from futures_fund.orchestration import preflight_step


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cycle", type=int, required=True)
    args = ap.parse_args()
    settings = load_settings()
    ex = FuturesExchange.from_settings(settings)
    ctx = preflight_step(ex, settings, "state", "memory",
                         now=datetime.now(timezone.utc), cycle_no=args.cycle)
    save_output("state", args.cycle, "context", ctx)
    print(json.dumps(ctx, indent=2, default=str))


if __name__ == "__main__":
    main()
```

`scripts/screen_cli.py`:

```python
"""Phase 4.5 CLI: read analyst reports, write the screened top-N symbols.

    uv run python scripts/screen_cli.py --cycle N --top 5
"""
from __future__ import annotations

import argparse
import json

from futures_fund.cycle_io import load_output, save_output
from futures_fund.orchestration import screen_step


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cycle", type=int, required=True)
    ap.add_argument("--top", type=int, default=5)
    args = ap.parse_args()
    reports = load_output("state", args.cycle, "analyst_reports")
    symbols = screen_step(reports, top_n=args.top)
    save_output("state", args.cycle, "screened", {"symbols": symbols})
    print(json.dumps({"symbols": symbols}, indent=2))


if __name__ == "__main__":
    main()
```

`scripts/gate_execute_cli.py`:

```python
"""Phases 7-10 CLI: gate + consolidate + execute the trader proposals; persist + report.

    uv run python scripts/gate_execute_cli.py --cycle N
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

from futures_fund.config import load_settings
from futures_fund.cycle_io import load_output, save_output
from futures_fund.exchange import FuturesExchange
from futures_fund.orchestration import gate_execute_step


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cycle", type=int, required=True)
    args = ap.parse_args()
    settings = load_settings()
    ex = FuturesExchange.from_settings(settings)
    proposals = load_output("state", args.cycle, "proposals")["proposals"]
    report = gate_execute_step(ex, settings, "state", "memory",
                               now=datetime.now(timezone.utc), cycle_no=args.cycle,
                               proposals=proposals)
    save_output("state", args.cycle, "report", report)
    print(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    main()
```

`scripts/reflect_cli.py`:

```python
"""Reflection CLI: emit the winners/losers payload for the Reflector subagent.

    uv run python scripts/reflect_cli.py --cycle N
"""
from __future__ import annotations

import argparse
import json

from futures_fund.cycle_io import save_output
from futures_fund.orchestration import reflect_step


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cycle", type=int, required=True)
    args = ap.parse_args()
    payload = reflect_step("memory")
    save_output("state", args.cycle, "reflection_input", payload)
    print(json.dumps(payload, indent=2, default=str))


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run** `uv run pytest tests/test_dry_run.py -v` — expect PASS (1 passed). Then `uv run ruff check scripts/ tests/test_dry_run.py` — fix style only. Do NOT run the CLIs (they need network).

- [ ] **Step 5: Commit**

```bash
git add scripts/preflight.py scripts/screen_cli.py scripts/gate_execute_cli.py scripts/reflect_cli.py tests/test_dry_run.py
git commit -m "feat: orchestration CLIs + dry-run e2e (fixture agents -> executed cycle, no LLM)"
```

---

## Task 4: Schema-conformance fixtures + test

**Files:** create `tests/fixtures/agent_examples/*.json` (8 files) and `tests/test_agent_conformance.py`.

Each role file (Task 6) will embed an example output identical to its fixture here; this test guarantees the contracts and the role files cannot drift.

- [ ] **Step 1: Create the fixture examples** under `tests/fixtures/agent_examples/`:

`watcher.json`:
```json
{"candidates": [
  {"symbol": "BTC/USDT:USDT", "lean": "long", "rationale": "leading the risk-on move; clean uptrend", "score": 0.82, "correlation_group": "majors"},
  {"symbol": "ETH/USDT:USDT", "lean": "long", "rationale": "following BTC, ETF flows", "score": 0.71, "correlation_group": "majors"},
  {"symbol": "SOL/USDT:USDT", "lean": "short", "rationale": "rejected at resistance, funding rich", "score": 0.64, "correlation_group": "alt-l1"}
]}
```
`technical.json`:
```json
{"agent": "technical", "symbol": "BTCUSDT", "stance": "bullish", "confidence": 0.74,
 "key_points": ["price above rising 20/50 EMA", "higher highs on 4h", "ATR expanding with trend"],
 "signals": {"ema_slope": 0.012, "rsi": 61.5, "atr": 850.0, "adx": 28.0}}
```
`derivatives.json`:
```json
{"agent": "derivatives", "symbol": "BTCUSDT", "stance": "bullish", "confidence": 0.66,
 "key_points": ["OI rising with price (new longs)", "funding mildly positive, not crowded", "long/short ratio neutral"],
 "signals": {"funding_rate": 0.0001, "oi_change_pct": 0.04, "long_short_ratio": 1.1}}
```
`news.json`:
```json
{"agent": "news", "symbol": "BTCUSDT", "stance": "bullish", "confidence": 0.55,
 "key_points": ["spot ETF net inflows reported", "no adverse regulatory headlines"],
 "signals": {"catalyst_count": 2, "risk_off_flag": 0}}
```
`sentiment.json`:
```json
{"agent": "sentiment", "symbol": "BTCUSDT", "stance": "neutral", "confidence": 0.5,
 "key_points": ["Fear&Greed 61 (greed) - mild contrarian caution", "macro: DXY soft, yields stable"],
 "signals": {"fear_greed": 61, "dxy_trend": -1, "social_attention": 0.4}}
```
`research_plan.json`:
```json
{"symbol": "BTCUSDT", "rating": "long", "confidence": 0.7,
 "thesis": "Technical + derivatives align bullish; news supportive; sentiment only mild caution. Bull case (trend continuation on rising OI) outweighs bear (greed/overbought) given low-vol uptrend regime.",
 "falsifiable_prediction": "BTC holds above the 20EMA and makes a higher high within 2 cycles; invalidated by a 4h close below the prior swing low."}
```
`trader.json`:
```json
{"symbol": "BTCUSDT", "direction": "long", "entry": 73500.0, "stop": 71800.0,
 "take_profits": [76900.0], "atr": 850.0, "confidence": 0.7, "horizon_hours": 8,
 "rationale": "long per RM plan; 2x ATR stop, 2R target; entry on confirmation of trend continuation",
 "confirmation": true}
```
`reflector.json`:
```json
{"lessons": [
  {"text": "In low-vol uptrends, mild greed (F&G 60-70) is not a reason to fade - trend continued.",
   "regime": "low_vol_trend", "tags": ["sentiment", "trend"], "importance": 6,
   "provenance": ["<decision_id>"]}
]}
```

- [ ] **Step 2: Write the test** — `tests/test_agent_conformance.py`:

```python
import json
from pathlib import Path

import pytest

from futures_fund.contracts import (
    AgentProposal,
    AnalystReport,
    ResearchPlan,
    WatcherOutput,
)
from futures_fund.lessons import Lesson

FIX = Path(__file__).parent / "fixtures" / "agent_examples"


def _load(name):
    return json.loads((FIX / name).read_text())


def test_watcher_example_conforms():
    WatcherOutput.model_validate(_load("watcher.json"))


@pytest.mark.parametrize("name", ["technical.json", "derivatives.json", "news.json", "sentiment.json"])
def test_analyst_examples_conform(name):
    r = AnalystReport.model_validate(_load(name))
    assert r.stance in {"bullish", "bearish", "neutral"}


def test_research_plan_example_conforms():
    p = ResearchPlan.model_validate(_load("research_plan.json"))
    assert p.rating in {"strong_long", "long", "flat", "short", "strong_short"}
    assert p.falsifiable_prediction


def test_trader_example_conforms():
    ap = AgentProposal.model_validate(_load("trader.json"))
    assert ap.symbol == "BTCUSDT" and ap.direction == "long"


def test_reflector_example_lessons_conform():
    data = _load("reflector.json")
    from datetime import datetime, timezone
    for lz in data["lessons"]:
        Lesson.model_validate({**lz, "ts": datetime(2026, 5, 1, tzinfo=timezone.utc)})
```

- [ ] **Step 3: Run** `uv run pytest tests/test_agent_conformance.py -v` — expect PASS (8 passed: 1 watcher + 4 analyst + 1 plan + 1 trader + 1 reflector). Then `uv run ruff check tests/test_agent_conformance.py`.

- [ ] **Step 4: Commit**

```bash
git add tests/fixtures/agent_examples/ tests/test_agent_conformance.py
git commit -m "test: agent-output example fixtures conform to B1 contracts"
```

---

## Task 5: Author the `SKILL.md` orchestrator playbook

**Files:** create `SKILL.md` (repo root).

- [ ] **Step 1: Write `SKILL.md`** with EXACTLY this content (it is the playbook the orchestrator Claude follows):

````markdown
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
Run: `uv run python scripts/reflect_cli.py --cycle N` → `state/cycle/N/reflection_input.json` (winners vs losers). If there are closed trades, dispatch the **Reflector** (opus, `agents/reflector.md`) with that payload → CANDIDATE lessons; the orchestrator records them (lessons are only *proposed* now — promotion to VALIDATED is gated by the eval harness in Phase C). Finally, present the `report.json` to the user: actions taken, current book, equity, risk posture.

## Subagent dispatch rules
- Inject `MISSION.md` verbatim at the top of EVERY subagent prompt.
- Give each subagent ONLY its role file's inputs + the relevant cycle JSON; never your full context.
- Each subagent must return ONLY valid JSON matching its contract. If a subagent returns malformed JSON, re-dispatch once with the validation error; if it fails again, log it, skip that symbol/agent, and continue (cap conviction — never trade on missing analysis).
- Retrieve relevant lessons for the debate/trader prompts (regime-filtered, top 3-7) so the team learns from the past.

## Self-healing (see B3 / spec §5)
If any `scripts/*` call errors: capture it to `state/error-log.jsonl`, diagnose the root cause (use systematic-debugging), fix the code, verify, commit, and append the repair to `memory/repair-journal.md` — then resume. NEVER weaken a risk limit or the execution path to make an error disappear; if you cannot fix it safely, set the HALT flag and surface.
````

- [ ] **Step 2: Verify it parses** — `python -c "import pathlib; t=pathlib.Path('SKILL.md').read_text(); assert t.startswith('---') and 'futures-fund' in t and 'Phase 7-10' in t; print('SKILL.md ok')"`.

- [ ] **Step 3: Commit**

```bash
git add SKILL.md
git commit -m "feat: SKILL.md orchestrator playbook (phased cycle choreography)"
```

---

## Task 6: Author the `agents/*.md` role files

**Files:** create the 12 role files under `agents/`. Each follows this **template** and references its B1 contract; its `## Output` example must be JSON identical in shape to the matching `tests/fixtures/agent_examples/*.json` (Task 4 guarantees those validate).

**Role-file template (use for every file):**
```markdown
# <Agent Name>

## Mission
You serve Operation TEMPEST (the charter is injected above). <one line on this agent's purpose>.

## Inputs
<what cycle JSON / brief / lessons this agent receives>

## How you think
<3-6 bullets of role-specific reasoning guidance — what signals, what biases to avoid>

## Output (return ONLY this JSON, no prose)
<the exact JSON schema this agent must emit, matching its B1 contract>

## Example
<a concrete example identical in shape to tests/fixtures/agent_examples/<x>.json>
```

- [ ] **Step 1: Author each role file** (12 files). Required per-file specifics:

  - `agents/watcher.md` → emits `WatcherOutput` (candidates: ~10, lean long/short/watch, diversification-aware: penalize over-correlated picks, prefer liquid majors + a few uncorrelated setups). Inputs: market context. Mindset: cast a wide net, but correlated longs count as one bet.
  - `agents/technical.md` → `AnalystReport` (agent="technical") per shortlisted symbol. Signals: EMA slope, RSI, ATR, ADX, support/resistance, structure. Avoid: trading counter-trend in strong trends.
  - `agents/derivatives.md` → `AnalystReport` (agent="derivatives"). Signals: funding rate (crowding/carry), OI change (new money vs short covering), long/short ratio, basis, liquidation clusters. The futures-native edge.
  - `agents/news.md` → `AnalystReport` (agent="news"). Catalysts (listings/hacks/regulatory/ETF), set `risk_off_flag`. Avoid: stale/duplicate headlines.
  - `agents/sentiment.md` → `AnalystReport` (agent="sentiment"). Fear&Greed (contrarian), social attention, FRED macro (DXY/yields/Fed). De-risk into FOMC/CPI.
  - `agents/bull.md` → emits a JSON `{"symbol","thesis","key_points","confidence"}` (free debate output; not a strict contract — schema-light). Strongest LONG case; engage the bear's points if present.
  - `agents/bear.md` → same shape; strongest SHORT/FLAT case; must rebut the bull's latest argument, not just list data.
  - `agents/research_manager.md` → `ResearchPlan` (the judge: weigh the debate, commit to a 5-tier rating + a falsifiable prediction; reserve `flat` only when genuinely balanced).
  - `agents/trader.md` → `AgentProposal` (convert the plan to a concrete order: entry, ATR stop 1.5-3x, take-profit at >=2R, confirmation trigger; leverage is the gate's output, not yours).
  - `agents/risk_manager.md` → DOCUMENTS that risk is the **deterministic gate** (`scripts/gate_execute_cli.py` → A1 `risk_gate`): adaptive regime×health caps, liq-distance >=2.5x stop, RR>=2, heat cap, circuit breakers. The agent's role is advisory only; the code gate is final and cannot be argued past. (No JSON contract — this file guides the orchestrator + documents the survival rule.)
  - `agents/portfolio_manager.md` → DOCUMENTS that consolidation is **deterministic** (B1 `consolidate`: gross-heat cap, CVaR de-risk, drop dust, correlated-as-one). Advisory only.
  - `agents/reflector.md` → emits `{"lessons": [<Lesson-shaped objects>]}` contrasting winners vs losers; low-level ("was the read right?") vs high-level ("was the action/sizing right?"); numeric deltas for quant/risk, prose for narrative agents; cite provenance decision ids. Lessons are CANDIDATE only.

- [ ] **Step 2: Write the role-file structural test** — `tests/test_role_files.py`:

```python
from pathlib import Path

import pytest

ROLES = ["watcher", "technical", "derivatives", "news", "sentiment", "bull", "bear",
         "research_manager", "trader", "risk_manager", "portfolio_manager", "reflector"]


@pytest.mark.parametrize("role", ROLES)
def test_role_file_exists_and_has_sections(role):
    p = Path("agents") / f"{role}.md"
    assert p.exists(), f"missing role file: {p}"
    text = p.read_text()
    assert "## Mission" in text
    # analyst/decision agents must specify an Output contract; the two deterministic docs are exempt
    if role not in ("risk_manager", "portfolio_manager"):
        assert "## Output" in text, f"{role} missing Output section"


def test_mission_file_exists_and_is_the_charter():
    t = Path("MISSION.md").read_text()
    assert "OPERATION TEMPEST" in t and "5%" in t
```

- [ ] **Step 3: Run** `uv run pytest tests/test_role_files.py -v` — expect PASS (13 passed: 12 role files + 1 mission). Then `uv run ruff check .` (ruff ignores markdown; just confirm no python regressions).

- [ ] **Step 4: Run the FULL suite:** `uv run pytest`. Report the EXACT total (expected 153 + cycle_io 5 + orchestration 5 + dry_run 1 + conformance 8 + role_files 13 = **185**).

- [ ] **Step 5: Commit**

```bash
git add agents/ tests/test_role_files.py
git commit -m "feat: agents/*.md role files (Watcher, 4 analysts, Bull/Bear, judges, Reflector)"
```

---

## Self-Review (completed during planning)

**Spec coverage (§3 roster, §3.1 funnel, §4 cycle, §6 memory):** the full orchestration choreography ✓ (SKILL.md, T5); all role files incl. the deterministic Risk/Portfolio Manager docs ✓ (T6); the analyst→screen→debate→judge→trader→gate funnel ✓ (orchestration + SKILL.md); per-cycle JSON bus + no-silent-runs ✓ (cycle_io, T1); the dry-run proving the plumbing without LLM ✓ (T3); schema conformance locking role files to contracts ✓ (T4); reflection payload to the Reflector ✓ (orchestration + reflector.md). Deferred to B3 (correct): the self-healing fix-loop mechanics (documented in SKILL.md, automated in B3) and lesson promotion gating (C).

**Placeholder scan:** the Python tasks (1-4) have complete code/fixtures. Task 6 uses a template + precise per-file specifics (the role files are authored prose, gated by the conformance + structural tests) — this is the appropriate form for prompt artifacts, not a code placeholder.

**Type/interface consistency:** orchestration uses B1's `fetch_context`/`audit_and_reflect`/`execute_proposals` with `agent_key="team"` (so the team's hit-rate is separate from the baseline — the B1 seam). `screen_step` parses raw dicts into `AnalystReport`. `gate_execute_step` converts `AgentProposal`→`TradeProposal` via B1's `to_trade_proposal`, injecting funding from context. Conformance fixtures validate against the exact B1 contracts. The dry-run mirrors the SKILL.md phase order.

**Note on rationale traceability (from B1 review):** `gate_execute_step` currently passes `contributing_agents=["research_manager","trader"]`; per-proposal `rationale` is captured but not yet threaded into the journal — a small enhancement to make in B3 (pass `rationale` into `append_decision`). Flagged, not blocking B2.

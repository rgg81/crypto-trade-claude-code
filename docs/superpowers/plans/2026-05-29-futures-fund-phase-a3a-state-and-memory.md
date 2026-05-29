# Futures-Fund Phase A3a — State & Memory Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the durable, git-versioned state & memory layer the cycle needs: account/positions/HALT persistence, paper-portfolio valuation (mark-to-market → `PortfolioHealth`), the two-phase decision journal, a per-agent hit-rate tracker, and the on-disk memory layout.

**Architecture:** Plain pydantic models + JSON/JSONL file persistence under two roots — `state/` (gitignored runtime: account, positions, HALT) and `memory/` (git-versioned: episodic journal, beliefs/lessons/playbook, hit-rate). Every function takes its directory as an argument so the whole layer is testable against `tmp_path` with no network and no globals. Valuation reuses A1's `position_risk` and `PortfolioHealth`.

**Tech Stack:** Python 3.11 / uv, pydantic v2, pytest, ruff. No network.

**Reference:** spec `docs/superpowers/specs/2026-05-29-futures-fund-design.md` §6 (memory & reflection). A1 (`futures_fund/models.py`: `Direction`, `PortfolioHealth`; `futures_fund/portfolio_risk.py`: `position_risk`) and A2 are merged on `main`.

**Conventions:**
- `state/` is **gitignored** (already in `.gitignore`); `memory/` is committed.
- Unrealized PnL: long `qty·(mark − entry)`, short `qty·(entry − mark)`; `qty` is always positive, direction carries the sign.
- Total equity = wallet `balance` + Σ unrealized PnL of open positions (isolated margin doesn't reduce equity; it only sets liquidation distance, handled in A1/A3b).
- Datetimes are timezone-aware UTC; persisted via pydantic `model_dump(mode="json")` (ISO strings) and read back as dicts.

---

## File Structure

```
futures_fund/
  state.py        # Position, AccountState models; load/save account & positions; HALT flag
  portfolio.py    # paper valuation: unrealized_pnl, total_equity, open_heat, portfolio_health
  journal.py      # Decision/Outcome models; append_decision, patch_outcome, read_open/read_all
  hitrate.py      # HitRateTracker: record_outcome, hit_rate (rolling window), persistence
  memory_layout.py# ensure_memory_layout + path helpers (episodic/semantic/procedural/lessons/hitrate)
tests/
  test_state.py
  test_portfolio.py
  test_journal.py
  test_hitrate.py
  test_memory_layout.py
```

---

## Task 1: State models + account/positions/HALT persistence

**Files:** create `futures_fund/state.py`, `tests/test_state.py`.

- [ ] **Step 1: Write the failing test** — `tests/test_state.py`:

```python
from datetime import datetime, timezone

from futures_fund.state import (
    AccountState,
    Position,
    is_halted,
    load_account,
    load_positions,
    save_account,
    save_positions,
    set_halt,
)


def _pos(symbol="BTCUSDT", direction="long"):
    return Position(
        symbol=symbol, direction=direction, qty=0.5, entry=100.0, stop=95.0,
        take_profits=[115.0], leverage=5.0, margin=10.0, liq_price=82.0,
        opened_cycle=1, opened_ts=datetime(2026, 5, 29, tzinfo=timezone.utc),
        decision_id="abc",
    )


def test_load_account_defaults_when_absent(tmp_path):
    acct = load_account(tmp_path, default_balance=10_000.0)
    assert acct.balance == 10_000.0
    assert acct.peak_equity == 10_000.0
    assert acct.halt is False


def test_save_then_load_account_roundtrip(tmp_path):
    save_account(tmp_path, AccountState(balance=12_345.0, peak_equity=13_000.0))
    acct = load_account(tmp_path, default_balance=10_000.0)
    assert acct.balance == 12_345.0
    assert acct.peak_equity == 13_000.0


def test_positions_roundtrip(tmp_path):
    save_positions(tmp_path, [_pos(), _pos("ETHUSDT", "short")])
    loaded = load_positions(tmp_path)
    assert len(loaded) == 2
    assert loaded[0].symbol == "BTCUSDT" and loaded[0].direction == "long"
    assert loaded[1].direction == "short"
    assert str(loaded[0].opened_ts.tzinfo) == "UTC"


def test_load_positions_empty_when_absent(tmp_path):
    assert load_positions(tmp_path) == []


def test_halt_flag_set_and_read(tmp_path):
    load_account(tmp_path, default_balance=10_000.0)  # ensure file
    assert is_halted(tmp_path) is False
    set_halt(tmp_path, True, reason="manual kill")
    assert is_halted(tmp_path) is True
    set_halt(tmp_path, False)
    assert is_halted(tmp_path) is False
```

- [ ] **Step 2: Run** `uv run pytest tests/test_state.py -v` — expect FAIL.

- [ ] **Step 3: Implement** `futures_fund/state.py`:

```python
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field

from futures_fund.models import Direction


class Position(BaseModel):
    symbol: str
    direction: Direction
    qty: float
    entry: float
    stop: float
    take_profits: list[float] = Field(default_factory=list)
    leverage: float
    margin: float
    liq_price: float
    opened_cycle: int
    opened_ts: datetime
    decision_id: str | None = None


class AccountState(BaseModel):
    balance: float          # realized USDT wallet balance
    peak_equity: float      # peak of total equity (balance + unrealized) ever seen
    halt: bool = False
    halt_reason: str = ""
    updated_ts: datetime | None = None


def _account_path(state_dir) -> Path:
    return Path(state_dir) / "account.json"


def _positions_path(state_dir) -> Path:
    return Path(state_dir) / "positions.json"


def load_account(state_dir, default_balance: float) -> AccountState:
    p = _account_path(state_dir)
    if p.exists():
        return AccountState.model_validate_json(p.read_text())
    return AccountState(balance=default_balance, peak_equity=default_balance)


def save_account(state_dir, account: AccountState) -> None:
    p = _account_path(state_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(account.model_dump_json(indent=2))


def load_positions(state_dir) -> list[Position]:
    p = _positions_path(state_dir)
    if not p.exists():
        return []
    raw = json.loads(p.read_text())
    return [Position.model_validate(r) for r in raw]


def save_positions(state_dir, positions: list[Position]) -> None:
    p = _positions_path(state_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps([json.loads(pos.model_dump_json()) for pos in positions], indent=2))


def is_halted(state_dir) -> bool:
    p = _account_path(state_dir)
    if not p.exists():
        return False
    return AccountState.model_validate_json(p.read_text()).halt


def set_halt(state_dir, halt: bool, reason: str = "") -> None:
    # operates on the persisted account; balance/peak default to 0 only if no account exists yet
    p = _account_path(state_dir)
    acct = AccountState.model_validate_json(p.read_text()) if p.exists() else AccountState(balance=0.0, peak_equity=0.0)
    acct.halt = halt
    acct.halt_reason = reason if halt else ""
    save_account(state_dir, acct)
```

- [ ] **Step 4: Run** `uv run pytest tests/test_state.py -v` — expect PASS (5 passed). Then `uv run ruff check futures_fund/state.py tests/test_state.py` — fix style only.

- [ ] **Step 5: Commit**

```bash
git add futures_fund/state.py tests/test_state.py
git commit -m "feat: account/position state models + JSON persistence + HALT flag"
```

---

## Task 2: Paper-portfolio valuation

**Files:** create `futures_fund/portfolio.py`, `tests/test_portfolio.py`.

- [ ] **Step 1: Write the failing test** — `tests/test_portfolio.py`:

```python
import pytest

from futures_fund.models import PortfolioHealth
from futures_fund.portfolio import open_heat, portfolio_health, total_equity, unrealized_pnl
from tests.test_state import _pos  # reuse the Position factory


def test_unrealized_long_and_short():
    long = _pos("BTCUSDT", "long")        # qty 0.5, entry 100
    short = _pos("ETHUSDT", "short")      # qty 0.5, entry 100
    assert unrealized_pnl(long, mark=110.0) == pytest.approx(5.0)    # 0.5*(110-100)
    assert unrealized_pnl(short, mark=110.0) == pytest.approx(-5.0)  # 0.5*(100-110)


def test_total_equity_adds_unrealized():
    positions = [_pos("BTCUSDT", "long")]   # +5 at mark 110
    eq = total_equity(balance=10_000.0, positions=positions, prices={"BTCUSDT": 110.0})
    assert eq == pytest.approx(10_005.0)


def test_total_equity_skips_missing_price():
    positions = [_pos("BTCUSDT", "long")]
    eq = total_equity(balance=10_000.0, positions=positions, prices={})  # no price -> 0 unrealized
    assert eq == pytest.approx(10_000.0)


def test_open_heat_uses_position_risk():
    # qty 0.5, stop gap 5 -> risk 2.5 on equity 10k = 0.00025
    positions = [_pos("BTCUSDT", "long")]
    assert open_heat(positions, equity=10_000.0) == pytest.approx(0.00025)


def test_portfolio_health_tracks_peak_and_drawdown():
    positions = [_pos("BTCUSDT", "long")]   # mark below entry -> loss
    h = portfolio_health(balance=10_000.0, peak_equity=10_050.0, positions=positions,
                         prices={"BTCUSDT": 90.0}, recent_hit_rate=0.6)
    assert isinstance(h, PortfolioHealth)
    # equity = 10000 + 0.5*(90-100) = 9995; peak stays 10050
    assert h.equity == pytest.approx(9995.0)
    assert h.peak_equity == 10_050.0
    assert h.recent_hit_rate == 0.6
    assert h.drawdown_from_peak == pytest.approx((10_050 - 9_995) / 10_050)


def test_portfolio_health_raises_peak_when_equity_higher():
    positions = [_pos("BTCUSDT", "long")]
    h = portfolio_health(balance=10_000.0, peak_equity=9_000.0, positions=positions,
                         prices={"BTCUSDT": 120.0}, recent_hit_rate=0.5)
    assert h.peak_equity == h.equity  # new high-water mark
```

- [ ] **Step 2: Run** `uv run pytest tests/test_portfolio.py -v` — expect FAIL.

- [ ] **Step 3: Implement** `futures_fund/portfolio.py`:

```python
from __future__ import annotations

from futures_fund.models import PortfolioHealth
from futures_fund.portfolio_risk import position_risk
from futures_fund.state import Position


def unrealized_pnl(position: Position, mark: float) -> float:
    if position.direction == "long":
        return position.qty * (mark - position.entry)
    return position.qty * (position.entry - mark)


def total_equity(balance: float, positions: list[Position], prices: dict[str, float]) -> float:
    """Wallet balance + unrealized PnL of open positions (skips positions with no price)."""
    upnl = 0.0
    for p in positions:
        mark = prices.get(p.symbol)
        if mark is not None:
            upnl += unrealized_pnl(p, mark)
    return balance + upnl


def open_heat(positions: list[Position], equity: float) -> float:
    """Sum of per-position stop-out risk as a fraction of equity (reuses A1 position_risk)."""
    return sum(position_risk(p.qty, p.entry, p.stop, equity) for p in positions)


def portfolio_health(
    balance: float, peak_equity: float, positions: list[Position],
    prices: dict[str, float], recent_hit_rate: float = 0.5,
) -> PortfolioHealth:
    """Compute A1's PortfolioHealth from live marks, raising the high-water mark if exceeded."""
    equity = total_equity(balance, positions, prices)
    return PortfolioHealth(
        equity=equity,
        peak_equity=max(peak_equity, equity),
        open_heat=open_heat(positions, equity) if equity > 0 else 0.0,
        recent_hit_rate=recent_hit_rate,
    )
```

- [ ] **Step 4: Run** `uv run pytest tests/test_portfolio.py -v` — expect PASS (6 passed). Then `uv run ruff check futures_fund/portfolio.py tests/test_portfolio.py` — fix style only. (Importing the `_pos` factory from `tests.test_state` is intentional; if ruff flags it, leave it — `tests/__init__.py` exists so the import resolves.)

- [ ] **Step 5: Commit**

```bash
git add futures_fund/portfolio.py tests/test_portfolio.py
git commit -m "feat: paper-portfolio valuation (unrealized, equity, heat, health)"
```

---

## Task 3: Two-phase decision journal

**Files:** create `futures_fund/journal.py`, `tests/test_journal.py`.

- [ ] **Step 1: Write the failing test** — `tests/test_journal.py`:

```python
from datetime import datetime, timezone

from futures_fund.journal import (
    Decision,
    append_decision,
    journal_file,
    patch_outcome,
    read_all_decisions,
    read_open_decisions,
)


def _decision(**over):
    base = dict(
        ts=datetime(2026, 5, 29, 12, tzinfo=timezone.utc), cycle=1, symbol="BTCUSDT",
        direction="long", entry=100.0, stop=95.0, confidence=0.7,
        rationale="momentum breakout", dominant_signal="trend",
    )
    base.update(over)
    return base


def test_append_returns_id_and_writes_monthly_file(tmp_path):
    did = append_decision(tmp_path, _decision())
    assert isinstance(did, str) and did
    f = journal_file(tmp_path, datetime(2026, 5, 29, tzinfo=timezone.utc))
    assert f.exists() and f.name == "journal-2026-05.jsonl"
    recs = read_all_decisions(tmp_path)
    assert len(recs) == 1 and recs[0]["id"] == did and recs[0]["symbol"] == "BTCUSDT"


def test_open_decisions_excludes_closed(tmp_path):
    d1 = append_decision(tmp_path, _decision(symbol="BTCUSDT"))
    append_decision(tmp_path, _decision(symbol="ETHUSDT"))
    assert len(read_open_decisions(tmp_path)) == 2
    ok = patch_outcome(tmp_path, d1, {
        "exit_ts": datetime(2026, 5, 30, tzinfo=timezone.utc), "realized_pnl": 42.0,
        "fees": 1.0, "prediction_correct": True, "low_level_lesson": "read was right",
    })
    assert ok is True
    opens = read_open_decisions(tmp_path)
    assert len(opens) == 1 and opens[0]["symbol"] == "ETHUSDT"


def test_patch_merges_outcome_fields(tmp_path):
    did = append_decision(tmp_path, _decision())
    patch_outcome(tmp_path, did, {"realized_pnl": -10.0, "prediction_correct": False})
    rec = next(r for r in read_all_decisions(tmp_path) if r["id"] == did)
    assert rec["realized_pnl"] == -10.0
    assert rec["prediction_correct"] is False
    assert rec["rationale"] == "momentum breakout"  # Phase-1 field preserved


def test_patch_unknown_id_returns_false(tmp_path):
    append_decision(tmp_path, _decision())
    assert patch_outcome(tmp_path, "nonexistent", {"realized_pnl": 1.0}) is False


def test_decision_model_allows_extra_agent_fields():
    # Phase B agents attach extra fields; the model must tolerate them
    d = Decision(id="x", ts=datetime(2026, 5, 29, tzinfo=timezone.utc), cycle=1,
                 symbol="BTCUSDT", direction="long", entry=100.0, stop=95.0,
                 bull_thesis="...", some_future_field=123)
    dumped = d.model_dump()
    assert dumped["bull_thesis"] == "..." and dumped["some_future_field"] == 123
```

- [ ] **Step 2: Run** `uv run pytest tests/test_journal.py -v` — expect FAIL.

- [ ] **Step 3: Implement** `futures_fund/journal.py`:

```python
from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from futures_fund.models import Direction


class Decision(BaseModel):
    """Two-phase decision record. Phase-1 fields written at decision time; Phase-2 (outcome)
    fields patched on close. extra='allow' lets Phase-B agents attach richer context."""

    model_config = ConfigDict(extra="allow")

    id: str
    ts: datetime
    cycle: int
    symbol: str
    direction: Direction
    entry: float
    stop: float
    # Phase-1 optional context
    take_profit: list[float] = Field(default_factory=list)
    size: float | None = None
    leverage: float | None = None
    r_multiple: float | None = None
    funding_at_entry: float | None = None
    regime: str | None = None
    confidence: float | None = None
    rationale: str | None = None
    dominant_signal: str | None = None
    contributing_agents: list[str] = Field(default_factory=list)
    retrieved_memory_ids: list[str] = Field(default_factory=list)
    # Phase-2 outcome (None until closed)
    exit_ts: datetime | None = None
    realized_pnl: float | None = None
    fees: float | None = None
    funding_paid: float | None = None
    slippage: float | None = None
    prediction_correct: bool | None = None
    low_level_lesson: str | None = None
    high_level_lesson: str | None = None
    importance: int | None = None


def _episodic_dir(memory_dir) -> Path:
    return Path(memory_dir) / "episodic"


def journal_file(memory_dir, ts: datetime) -> Path:
    return _episodic_dir(memory_dir) / f"journal-{ts:%Y-%m}.jsonl"


def append_decision(memory_dir, fields: dict) -> str:
    """Validate and append a Phase-1 decision; returns its id (generated if absent)."""
    data = dict(fields)
    data.setdefault("id", uuid.uuid4().hex)
    decision = Decision.model_validate(data)
    f = journal_file(memory_dir, decision.ts)
    f.parent.mkdir(parents=True, exist_ok=True)
    with f.open("a") as fh:
        fh.write(decision.model_dump_json() + "\n")
    return decision.id


def _all_files(memory_dir) -> list[Path]:
    d = _episodic_dir(memory_dir)
    return sorted(d.glob("journal-*.jsonl")) if d.exists() else []


def read_all_decisions(memory_dir) -> list[dict]:
    out: list[dict] = []
    for f in _all_files(memory_dir):
        for line in f.read_text().splitlines():
            if line.strip():
                out.append(json.loads(line))
    return out


def read_open_decisions(memory_dir) -> list[dict]:
    """Decisions without a realized outcome yet (Phase-2 not filled)."""
    return [r for r in read_all_decisions(memory_dir) if r.get("realized_pnl") is None]


def patch_outcome(memory_dir, decision_id: str, outcome: dict) -> bool:
    """Merge Phase-2 outcome fields into the decision with `decision_id`. Rewrites the
    containing monthly file. Returns False if the id is not found."""
    for f in _all_files(memory_dir):
        records = [json.loads(line) for line in f.read_text().splitlines() if line.strip()]
        hit = False
        for r in records:
            if r.get("id") == decision_id:
                # validate the merged record so outcome types are coerced (e.g. datetimes)
                merged = Decision.model_validate({**r, **outcome})
                r.clear()
                r.update(json.loads(merged.model_dump_json()))
                hit = True
        if hit:
            f.write_text("".join(json.dumps(r) + "\n" for r in records))
            return True
    return False
```

- [ ] **Step 4: Run** `uv run pytest tests/test_journal.py -v` — expect PASS (5 passed). Then `uv run ruff check futures_fund/journal.py tests/test_journal.py` — fix style only.

- [ ] **Step 5: Commit**

```bash
git add futures_fund/journal.py tests/test_journal.py
git commit -m "feat: two-phase decision journal (append/patch/read, monthly JSONL)"
```

---

## Task 4: Per-agent hit-rate tracker

**Files:** create `futures_fund/hitrate.py`, `tests/test_hitrate.py`.

- [ ] **Step 1: Write the failing test** — `tests/test_hitrate.py`:

```python
from futures_fund.hitrate import hit_rate, record_outcome


def test_unknown_agent_defaults_to_half(tmp_path):
    assert hit_rate(tmp_path, "watcher") == 0.5


def test_record_and_compute_hit_rate(tmp_path):
    for correct in [True, True, False, True]:   # 3/4
        record_outcome(tmp_path, "trend_analyst", correct)
    assert hit_rate(tmp_path, "trend_analyst") == 0.75


def test_rolling_window_keeps_only_recent(tmp_path):
    # 20 wins then 5 losses; window of 5 -> 0.0
    for _ in range(20):
        record_outcome(tmp_path, "a", True)
    for _ in range(5):
        record_outcome(tmp_path, "a", False)
    assert hit_rate(tmp_path, "a", window=5) == 0.0
    assert hit_rate(tmp_path, "a", window=25) == 20 / 25


def test_separate_agents_tracked_independently(tmp_path):
    record_outcome(tmp_path, "bull", True)
    record_outcome(tmp_path, "bear", False)
    assert hit_rate(tmp_path, "bull") == 1.0
    assert hit_rate(tmp_path, "bear") == 0.0
```

- [ ] **Step 2: Run** `uv run pytest tests/test_hitrate.py -v` — expect FAIL.

- [ ] **Step 3: Implement** `futures_fund/hitrate.py`:

```python
from __future__ import annotations

import json
from pathlib import Path

_MAX_HISTORY = 200  # cap stored outcomes per agent


def _scores_path(memory_dir) -> Path:
    return Path(memory_dir) / "hitrate" / "agent_scores.json"


def _load(memory_dir) -> dict[str, list[bool]]:
    p = _scores_path(memory_dir)
    return json.loads(p.read_text()) if p.exists() else {}


def _save(memory_dir, data: dict[str, list[bool]]) -> None:
    p = _scores_path(memory_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2))


def record_outcome(memory_dir, agent: str, correct: bool) -> None:
    data = _load(memory_dir)
    history = data.get(agent, [])
    history.append(bool(correct))
    data[agent] = history[-_MAX_HISTORY:]
    _save(memory_dir, data)


def hit_rate(memory_dir, agent: str, window: int = 30) -> float:
    """Rolling hit rate over the last `window` outcomes. Defaults to 0.5 with no history."""
    history = _load(memory_dir).get(agent, [])
    if not history:
        return 0.5
    recent = history[-window:]
    return sum(recent) / len(recent)
```

- [ ] **Step 4: Run** `uv run pytest tests/test_hitrate.py -v` — expect PASS (4 passed). Then `uv run ruff check futures_fund/hitrate.py tests/test_hitrate.py` — fix style only.

- [ ] **Step 5: Commit**

```bash
git add futures_fund/hitrate.py tests/test_hitrate.py
git commit -m "feat: per-agent rolling hit-rate tracker"
```

---

## Task 5: Memory layout scaffolding

**Files:** create `futures_fund/memory_layout.py`, `tests/test_memory_layout.py`.

- [ ] **Step 1: Write the failing test** — `tests/test_memory_layout.py`:

```python
from futures_fund.memory_layout import ensure_memory_layout, memory_paths


def test_ensure_creates_all_dirs_and_seed_files(tmp_path):
    paths = ensure_memory_layout(tmp_path)
    assert (tmp_path / "episodic").is_dir()
    assert (tmp_path / "hitrate").is_dir()
    assert paths["beliefs"].exists() and paths["beliefs"].name == "beliefs.md"
    assert paths["lessons"].exists() and paths["lessons"].name == "lessons.md"
    assert paths["playbook"].exists() and paths["playbook"].name == "playbook.md"
    # seed files are non-empty (have a heading)
    assert paths["beliefs"].read_text().strip().startswith("#")


def test_ensure_is_idempotent_and_preserves_content(tmp_path):
    paths = ensure_memory_layout(tmp_path)
    paths["lessons"].write_text("# Lessons\n\n- VALIDATED: don't fight strong funding\n")
    ensure_memory_layout(tmp_path)  # second call must not clobber
    assert "don't fight strong funding" in paths["lessons"].read_text()


def test_memory_paths_returns_expected_keys(tmp_path):
    p = memory_paths(tmp_path)
    assert set(p) >= {"episodic", "semantic", "procedural", "lessons", "beliefs", "playbook", "hitrate"}
```

- [ ] **Step 2: Run** `uv run pytest tests/test_memory_layout.py -v` — expect FAIL.

- [ ] **Step 3: Implement** `futures_fund/memory_layout.py`:

```python
from __future__ import annotations

from pathlib import Path

_SEED = {
    "beliefs": "# Beliefs\n\nEvolving per-symbol / per-regime beliefs. Each entry should cite the\n"
               "journal decision ids that support it.\n",
    "lessons": "# Lessons\n\nCANDIDATE and VALIDATED lessons with provenance. VALIDATED lessons become\n"
               "hard vetoes; demote aggressively when a regime shifts.\n",
    "playbook": "# Playbook\n\nThe team's standing trading rules (procedural memory).\n",
}


def memory_paths(memory_dir) -> dict[str, Path]:
    root = Path(memory_dir)
    return {
        "episodic": root / "episodic",
        "semantic": root / "semantic",
        "procedural": root / "procedural",
        "lessons": root / "lessons" / "lessons.md",
        "beliefs": root / "semantic" / "beliefs.md",
        "playbook": root / "procedural" / "playbook.md",
        "hitrate": root / "hitrate",
    }


def ensure_memory_layout(memory_dir) -> dict[str, Path]:
    """Create the memory directory tree and seed the markdown files if absent.
    Idempotent: never overwrites existing files."""
    paths = memory_paths(memory_dir)
    for key in ("episodic", "semantic", "procedural", "hitrate"):
        paths[key].mkdir(parents=True, exist_ok=True)
    paths["lessons"].parent.mkdir(parents=True, exist_ok=True)
    for key, seed in _SEED.items():
        if not paths[key].exists():
            paths[key].write_text(seed)
    return paths
```

- [ ] **Step 4: Run** `uv run pytest tests/test_memory_layout.py -v` — expect PASS (3 passed).

- [ ] **Step 5: Run the FULL suite + lint:** `uv run pytest` then `uv run ruff check .`. Report the EXACT total (expected 79 + state 5 + portfolio 6 + journal 5 + hitrate 4 + memory_layout 3 = **102**).

- [ ] **Step 6: Commit**

```bash
git add futures_fund/memory_layout.py tests/test_memory_layout.py
git commit -m "feat: memory layout scaffolding (episodic/semantic/procedural/lessons/hitrate)"
```

---

## Self-Review (completed during planning)

**Spec coverage (§6 memory & reflection + the state the cycle needs):** account/positions persistence + HALT ✓ (T1); paper valuation → `PortfolioHealth` feeding A1's adaptive matrix ✓ (T2); two-phase decision journal with Phase-1/Phase-2 split + monthly files ✓ (T3); per-agent hit-rate for meta-allocation ✓ (T4); on-disk memory layout (episodic/semantic/procedural/lessons + seeds) ✓ (T5). Deferred to A3b/B (correct): the executor that *produces* fills/outcomes to journal; lesson retrieval/scoring + reflection (Phase B); CVaR-into-gate + cluster-aware PM consolidation (A3b).

**Placeholder scan:** none — every step has runnable code/fixtures and exact commands.

**Type consistency:** `Position` (T1) is imported by `portfolio.py` (T2) and the `_pos` factory is reused by `test_portfolio.py`. `portfolio_health` returns A1's `PortfolioHealth` with its exact fields (equity, peak_equity, open_heat, recent_hit_rate). `open_heat` reuses A1's `position_risk(qty, entry, stop, equity)` signature. `Decision` (T3) tolerates extra fields (Phase B). Directory-argument convention is uniform across state/journal/hitrate/memory_layout, so the A3b cycle can pass `settings`-derived `state/` and `memory/` roots.

**Integration note for A3b:** the cycle will (1) `ensure_memory_layout(memory_dir)` at preflight, (2) `load_account`/`load_positions`, (3) compute `portfolio_health` from live marks → feed A1 `risk_gate`, (4) on close, `patch_outcome` + `record_outcome`, (5) honor `is_halted`. All entry points exist after this plan.

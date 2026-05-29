# Futures-Fund Phase B1 — Agent Integration Rails Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the testable Python "rails" that the (Phase-B2) Claude orchestrator runs around its subagent dispatches: the structured JSON **contracts** each agent must emit, **memory retrieval** (a lesson store + recency·importance·relevance scoring), per-symbol **data briefs** for analyst context, the analyst→**screen** funnel, the agent→execution **bridge** (so agent proposals run through the same A1 risk gate + A3b execution), and the **reflection** glue. All unit-tested with fixture agent-outputs — no live LLM.

**Architecture:** The desk is a Claude-native skill: at runtime the orchestrator (the Claude running `SKILL.md`, built in B2) dispatches subagents (Watcher, analysts, Bull/Bear, judges, Reflector), captures their JSON outputs, and calls these Python rails for everything deterministic. B1 makes that contract concrete and reusable: it defines the pydantic models the role files must produce, and refactors the A3b cycle so the back half (gate→consolidate→execute→journal) is a reusable `execute_proposals(...)` shared by both the baseline cycle and the agent cycle.

**Tech Stack:** Python 3.11 / uv, pydantic v2, pandas/numpy, pytest, ruff. No network, no LLM (agent outputs are fixtures in tests).

**Reference:** spec §3 (roster), §3.1 (dispatch funnel), §6 (memory & reflection). Builds on merged A1–A3 (esp. `models.TradeProposal`, `risk_gate`, `baseline.simple_regime`, `journal`, `cycle`).

**Key design decisions (within the approved architecture):**
- **Data flow:** subagents emit JSON validated against B1 contracts; the orchestrator persists them under `state/cycle/` and passes file paths to the rails. Tests feed the contracts directly.
- **Symbols:** `Candidate.symbol` is the ccxt *unified* symbol (e.g. `BTC/USDT:USDT`); `AgentProposal.symbol` is the *raw* exchange id (e.g. `BTCUSDT`) to match `SymbolSpec.symbol` and the A1 gate.
- **Lessons** become structured records (`memory/lessons/lessons.jsonl`) for machine retrieval, *in addition to* the human-readable `lessons.md`. Retrieval is tag-based to start (embeddings only if needed — spec §6).
- **5-tier rating → direction:** strong_long/long→long, strong_short/short→short, flat→no trade.

---

## File Structure

```
futures_fund/
  contracts.py     # pydantic agent-I/O models + rating->direction + AgentProposal->TradeProposal
  lessons.py       # Lesson model + JSONL store + score_lesson + retrieve_lessons
  brief.py         # build_symbol_brief: compact per-symbol data bundle for analyst prompts
  screen.py        # screen_reports: aggregate analyst stances -> top-N symbols for debate
  cycle.py         # (refactor) extract fetch_context / audit_and_reflect / execute_proposals
  reflect.py       # reflection payload builder + record_lesson (CANDIDATE)
tests/
  test_contracts.py · test_lessons.py · test_brief.py · test_screen.py
  test_execute_proposals.py · test_reflect.py
```

---

## Task 1: Agent I/O contracts

**Files:** create `futures_fund/contracts.py`, `tests/test_contracts.py`.

- [ ] **Step 1: Write the failing test** — `tests/test_contracts.py`:

```python
import pytest
from pydantic import ValidationError

from futures_fund.contracts import (
    AgentProposal,
    AnalystReport,
    Candidate,
    ResearchPlan,
    rating_to_direction,
    to_trade_proposal,
)
from futures_fund.models import TradeProposal


def test_candidate_rejects_bad_lean():
    with pytest.raises(ValidationError):
        Candidate(symbol="BTC/USDT:USDT", lean="sideways", rationale="x", score=0.5)


def test_rating_to_direction_maps_five_tiers():
    assert rating_to_direction("strong_long") == "long"
    assert rating_to_direction("long") == "long"
    assert rating_to_direction("short") == "short"
    assert rating_to_direction("strong_short") == "short"
    assert rating_to_direction("flat") is None


def test_research_plan_requires_falsifiable_prediction():
    with pytest.raises(ValidationError):
        ResearchPlan(symbol="BTCUSDT", rating="long", confidence=0.7, thesis="up only")


def test_analyst_report_allows_extra_signal_fields():
    r = AnalystReport(agent="technical", symbol="BTCUSDT", stance="bullish", confidence=0.6,
                      signals={"rsi": 62.0}, extra_note="breakout")
    assert r.signals["rsi"] == 62.0
    assert r.model_dump()["extra_note"] == "breakout"


def test_to_trade_proposal_maps_fields_and_injects_funding():
    ap = AgentProposal(symbol="BTCUSDT", direction="long", entry=100.0, stop=95.0,
                       take_profits=[115.0], atr=2.0, confidence=0.7, horizon_hours=8,
                       rationale="trend + funding tailwind")
    tp = to_trade_proposal(ap, funding_rate=0.0001)
    assert isinstance(tp, TradeProposal)
    assert tp.symbol == "BTCUSDT" and tp.direction == "long"
    assert tp.funding_rate == 0.0001
    assert tp.risk_per_unit == pytest.approx(5.0)
```

- [ ] **Step 2: Run** `uv run pytest tests/test_contracts.py -v` — expect FAIL.

- [ ] **Step 3: Implement** `futures_fund/contracts.py`:

```python
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from futures_fund.models import Direction, TradeProposal

Lean = Literal["long", "short", "watch"]
Rating = Literal["strong_long", "long", "flat", "short", "strong_short"]
Stance = Literal["bullish", "bearish", "neutral"]


class Candidate(BaseModel):
    symbol: str                       # ccxt unified symbol, e.g. BTC/USDT:USDT
    lean: Lean
    rationale: str
    score: float = Field(ge=0.0, le=1.0)
    correlation_group: str | None = None


class WatcherOutput(BaseModel):
    candidates: list[Candidate] = Field(default_factory=list)


class AnalystReport(BaseModel):
    model_config = ConfigDict(extra="allow")  # tolerate agent-specific signal fields
    agent: str                        # e.g. 'technical', 'derivatives', 'news', 'sentiment'
    symbol: str
    stance: Stance
    confidence: float = Field(ge=0.0, le=1.0)
    key_points: list[str] = Field(default_factory=list)
    signals: dict = Field(default_factory=dict)


class ResearchPlan(BaseModel):
    symbol: str
    rating: Rating
    confidence: float = Field(ge=0.0, le=1.0)
    thesis: str
    falsifiable_prediction: str


class AgentProposal(BaseModel):
    symbol: str                       # raw exchange id, e.g. BTCUSDT (matches SymbolSpec.symbol)
    direction: Direction
    entry: float
    stop: float
    take_profits: list[float]
    atr: float
    confidence: float = Field(ge=0.0, le=1.0)
    horizon_hours: float = 4.0
    rationale: str = ""
    confirmation: bool = True         # QuantAgent-style confirmation trigger


_RATING_DIRECTION: dict[str, Direction] = {
    "strong_long": "long", "long": "long", "short": "short", "strong_short": "short",
}


def rating_to_direction(rating: Rating) -> Direction | None:
    """5-tier research rating -> trade direction. 'flat' -> None (no trade)."""
    return _RATING_DIRECTION.get(rating)


def to_trade_proposal(ap: AgentProposal, funding_rate: float) -> TradeProposal:
    """Convert an agent's structured proposal into the A1 TradeProposal the risk gate consumes."""
    return TradeProposal(
        symbol=ap.symbol, direction=ap.direction, entry=ap.entry, stop=ap.stop,
        take_profits=ap.take_profits, atr=ap.atr, confidence=ap.confidence,
        horizon_hours=ap.horizon_hours, funding_rate=funding_rate,
    )
```

- [ ] **Step 4: Run** `uv run pytest tests/test_contracts.py -v` — expect PASS (5 passed). Then `uv run ruff check futures_fund/contracts.py tests/test_contracts.py`.

- [ ] **Step 5: Commit**

```bash
git add futures_fund/contracts.py tests/test_contracts.py
git commit -m "feat: agent I/O contracts (Candidate/AnalystReport/ResearchPlan/AgentProposal) + converters"
```

---

## Task 2: Lesson store + memory retrieval

**Files:** create `futures_fund/lessons.py`, `tests/test_lessons.py`.

- [ ] **Step 1: Write the failing test** — `tests/test_lessons.py`:

```python
from datetime import datetime, timedelta, timezone

import pytest

from futures_fund.lessons import (
    Lesson,
    append_lesson,
    read_lessons,
    retrieve_lessons,
    score_lesson,
)

UTC = timezone.utc


def _lesson(**over):
    base = dict(text="don't fight strong funding", regime="high_vol_trend",
                symbol="BTCUSDT", tags=["funding", "trend"], importance=8)
    base.update(over)
    return base


def test_append_returns_id_and_read_roundtrip(tmp_path):
    lid = append_lesson(tmp_path, _lesson(), ts=datetime(2026, 5, 1, tzinfo=UTC))
    lessons = read_lessons(tmp_path)
    assert len(lessons) == 1 and lessons[0].id == lid
    assert lessons[0].state == "candidate" and lessons[0].importance == 8


def test_score_combines_recency_importance_relevance():
    now = datetime(2026, 5, 2, tzinfo=UTC)
    recent = Lesson(id="a", ts=now - timedelta(hours=1), text="x", importance=10,
                    tags=["funding"])
    old = Lesson(id="b", ts=now - timedelta(hours=500), text="y", importance=10,
                 tags=["funding"])
    # same importance & relevance; the recent one must score higher
    assert score_lesson(recent, now, ["funding"]) > score_lesson(old, now, ["funding"])
    # tag overlap raises relevance
    s_match = score_lesson(recent, now, ["funding"])
    s_nomatch = score_lesson(recent, now, ["macro"])
    assert s_match > s_nomatch


def test_retrieve_filters_by_regime_then_ranks_top_k(tmp_path):
    now = datetime(2026, 5, 2, tzinfo=UTC)
    append_lesson(tmp_path, _lesson(text="trend lesson", regime="high_vol_trend",
                                    tags=["trend"]), ts=now - timedelta(hours=2))
    append_lesson(tmp_path, _lesson(text="range lesson", regime="low_vol_range",
                                    tags=["meanrev"]), ts=now - timedelta(hours=2))
    append_lesson(tmp_path, _lesson(text="universal", regime=None, tags=["risk"]),
                  ts=now - timedelta(hours=2))
    got = retrieve_lessons(tmp_path, now=now, regime="high_vol_trend",
                           query_tags=["trend"], k=5)
    texts = [lz.text for lz in got]
    assert "trend lesson" in texts        # matching regime
    assert "universal" in texts           # regime=None applies everywhere
    assert "range lesson" not in texts    # wrong regime filtered out


def test_retrieve_respects_top_k(tmp_path):
    now = datetime(2026, 5, 2, tzinfo=UTC)
    for i in range(10):
        append_lesson(tmp_path, _lesson(text=f"l{i}", regime=None, tags=["risk"]),
                      ts=now - timedelta(hours=i + 1))
    assert len(retrieve_lessons(tmp_path, now=now, regime="x", query_tags=["risk"], k=3)) == 3
```

- [ ] **Step 2: Run** `uv run pytest tests/test_lessons.py -v` — expect FAIL.

- [ ] **Step 3: Implement** `futures_fund/lessons.py`:

```python
from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

LessonState = Literal["candidate", "validated", "retired"]


class Lesson(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    ts: datetime
    text: str
    regime: str | None = None         # quadrant it applies to; None = applies in all regimes
    symbol: str | None = None
    tags: list[str] = Field(default_factory=list)
    importance: int = 5               # 1-10
    state: LessonState = "candidate"
    confirmations: int = 0
    provenance: list[str] = Field(default_factory=list)  # journal decision ids


def _store(memory_dir) -> Path:
    return Path(memory_dir) / "lessons" / "lessons.jsonl"


def append_lesson(memory_dir, fields: dict, ts: datetime) -> str:
    data = {**fields, "ts": ts}
    data.setdefault("id", uuid.uuid4().hex)
    lesson = Lesson.model_validate(data)
    p = _store(memory_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as fh:
        fh.write(lesson.model_dump_json() + "\n")
    return lesson.id


def read_lessons(memory_dir) -> list[Lesson]:
    p = _store(memory_dir)
    if not p.exists():
        return []
    return [Lesson.model_validate_json(line) for line in p.read_text().splitlines() if line.strip()]


def score_lesson(lesson: Lesson, now: datetime, query_tags: list[str],
                 w_rec: float = 1.0, w_imp: float = 1.0, w_rel: float = 1.0) -> float:
    """Generative-Agents-style score: recency (Ebbinghaus) + importance + tag relevance (Jaccard)."""
    hours = max(0.0, (now - lesson.ts).total_seconds() / 3600.0)
    recency = 0.995 ** hours
    importance = lesson.importance / 10.0
    qt, lt = set(query_tags), set(lesson.tags)
    relevance = len(qt & lt) / len(qt | lt) if (qt or lt) else 0.0
    return w_rec * recency + w_imp * importance + w_rel * relevance


def retrieve_lessons(memory_dir, now: datetime, regime: str | None,
                     query_tags: list[str], k: int = 5) -> list[Lesson]:
    """Regime-filter FIRST (a lesson with regime=None applies everywhere), then rank by score,
    return the top-k. Retired lessons are excluded."""
    candidates = [
        lz for lz in read_lessons(memory_dir)
        if lz.state != "retired" and (lz.regime is None or lz.regime == regime)
    ]
    candidates.sort(key=lambda lz: score_lesson(lz, now, query_tags), reverse=True)
    return candidates[:k]
```

- [ ] **Step 4: Run** `uv run pytest tests/test_lessons.py -v` — expect PASS (5 passed). Then `uv run ruff check futures_fund/lessons.py tests/test_lessons.py` (UP017: use `from datetime import UTC` in the test if flagged).

- [ ] **Step 5: Commit**

```bash
git add futures_fund/lessons.py tests/test_lessons.py
git commit -m "feat: structured lesson store + regime-filtered recency/importance/relevance retrieval"
```

---

## Task 3: Per-symbol data brief

**Files:** create `futures_fund/brief.py`, `tests/test_brief.py`.

- [ ] **Step 1: Write the failing test** — `tests/test_brief.py`:

```python
import numpy as np
import pandas as pd

from futures_fund.brief import build_symbol_brief


class FakeExchange:
    def __init__(self, df, funding_rate=0.0001):
        self.df = df
        self.funding_rate = funding_rate

    def ohlcv(self, symbol, timeframe="4h", limit=500):
        return self.df

    def funding(self, symbol):
        from datetime import datetime, timezone
        from futures_fund.market_data import FundingInfo
        return FundingInfo(symbol=symbol, current_rate=self.funding_rate,
                           next_funding_ts=datetime(2026, 1, 1, tzinfo=timezone.utc),
                           interval_hours=8.0, mark_price=float(self.df["close"].iloc[-1]),
                           index_price=float(self.df["close"].iloc[-1]))


def _uptrend(n=60):
    rng = np.random.default_rng(2)
    close = 100.0 + 0.7 * np.arange(n) + rng.normal(0, 0.05, n)
    return pd.DataFrame({
        "timestamp": pd.date_range("2026-01-01", periods=n, freq="4h", tz="UTC"),
        "open": close, "high": close + 0.2, "low": close - 0.2, "close": close, "volume": 1.0,
    })


def test_brief_has_expected_keys_and_types():
    b = build_symbol_brief(FakeExchange(_uptrend()), "BTC/USDT:USDT", timeframe="4h")
    assert b["symbol"] == "BTC/USDT:USDT"
    assert b["regime"] in {"low_vol_trend", "high_vol_trend", "low_vol_range",
                           "high_vol_range", "transition"}
    assert b["trend_direction"] == "up"
    assert isinstance(b["last_close"], float) and b["last_close"] > 0
    assert isinstance(b["atr"], float) and b["atr"] > 0
    assert isinstance(b["funding_rate"], float)
    assert "momentum_20" in b and isinstance(b["momentum_20"], float)


def test_brief_momentum_positive_on_uptrend():
    b = build_symbol_brief(FakeExchange(_uptrend()), "BTC/USDT:USDT")
    assert b["momentum_20"] > 0
```

- [ ] **Step 2: Run** `uv run pytest tests/test_brief.py -v` — expect FAIL.

- [ ] **Step 3: Implement** `futures_fund/brief.py`:

```python
from __future__ import annotations

from futures_fund.baseline import _atr, simple_regime


def build_symbol_brief(exchange, symbol: str, timeframe: str = "4h") -> dict:
    """Compact, JSON-serializable per-symbol data bundle the orchestrator injects into the
    analyst subagents' prompts. Pure-ish: reads only from the injected exchange."""
    df = exchange.ohlcv(symbol, timeframe)
    funding = exchange.funding(symbol)
    close = df["close"]
    last = float(close.iloc[-1])
    regime = simple_regime(df)
    mom_20 = float(close.iloc[-1] / close.iloc[-21] - 1.0) if len(close) > 21 else 0.0
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "last_close": last,
        "regime": regime.quadrant,
        "trend_direction": regime.trend_direction,
        "atr": float(_atr(df)),
        "momentum_20": mom_20,
        "funding_rate": float(funding.current_rate),
        "funding_interval_hours": float(funding.interval_hours),
        "mark_price": float(funding.mark_price),
    }
```

- [ ] **Step 4: Run** `uv run pytest tests/test_brief.py -v` — expect PASS (2 passed). Then `uv run ruff check futures_fund/brief.py tests/test_brief.py`. (Importing `_atr` from `baseline` is intentional reuse; it's an existing module-level function.)

- [ ] **Step 5: Commit**

```bash
git add futures_fund/brief.py tests/test_brief.py
git commit -m "feat: per-symbol data brief for analyst-agent context"
```

---

## Task 4: Analyst screen (the §3.1 funnel)

**Files:** create `futures_fund/screen.py`, `tests/test_screen.py`.

- [ ] **Step 1: Write the failing test** — `tests/test_screen.py`:

```python
from futures_fund.contracts import AnalystReport
from futures_fund.screen import screen_reports, symbol_conviction


def _r(symbol, stance, conf, agent="technical"):
    return AnalystReport(agent=agent, symbol=symbol, stance=stance, confidence=conf)


def test_symbol_conviction_nets_bullish_minus_bearish_weighted_by_agreement():
    reports = [_r("BTCUSDT", "bullish", 0.8, "technical"),
               _r("BTCUSDT", "bullish", 0.6, "derivatives"),
               _r("BTCUSDT", "neutral", 0.5, "news")]
    # net stance = +0.8 +0.6 +0 = 1.4; agreement = 2 bullish -> conviction = |1.4| * 2
    assert symbol_conviction(reports) == 1.4 * 2


def test_screen_keeps_top_n_by_conviction():
    reports = [
        _r("BTCUSDT", "bullish", 0.9, "technical"), _r("BTCUSDT", "bullish", 0.9, "derivatives"),
        _r("ETHUSDT", "bullish", 0.5, "technical"),
        _r("SOLUSDT", "bearish", 0.8, "technical"), _r("SOLUSDT", "bearish", 0.7, "derivatives"),
    ]
    top = screen_reports(reports, top_n=2)
    assert set(top) == {"BTCUSDT", "SOLUSDT"}     # ETH (single weak signal) screened out
    assert top[0] == "BTCUSDT"                     # strongest first


def test_screen_handles_fewer_than_n():
    top = screen_reports([_r("BTCUSDT", "bullish", 0.5)], top_n=5)
    assert top == ["BTCUSDT"]


def test_screen_drops_pure_neutral_symbols():
    top = screen_reports([_r("BTCUSDT", "neutral", 0.9), _r("BTCUSDT", "neutral", 0.8)], top_n=5)
    assert top == []     # zero net conviction -> not worth debating
```

- [ ] **Step 2: Run** `uv run pytest tests/test_screen.py -v` — expect FAIL.

- [ ] **Step 3: Implement** `futures_fund/screen.py`:

```python
from __future__ import annotations

from collections import defaultdict

from futures_fund.contracts import AnalystReport

_STANCE_SIGN = {"bullish": 1.0, "bearish": -1.0, "neutral": 0.0}


def symbol_conviction(reports: list[AnalystReport]) -> float:
    """Net directional stance (signed, confidence-weighted) times the number of agents who took
    a non-neutral side — rewards both strength and agreement."""
    net = sum(_STANCE_SIGN[r.stance] * r.confidence for r in reports)
    agreement = sum(1 for r in reports if r.stance != "neutral")
    return abs(net) * agreement


def screen_reports(reports: list[AnalystReport], top_n: int) -> list[str]:
    """Group analyst reports by symbol, rank by conviction, return the top-N symbols (strongest
    first). Symbols with zero conviction (all-neutral) are dropped — the §3.1 funnel."""
    by_symbol: dict[str, list[AnalystReport]] = defaultdict(list)
    for r in reports:
        by_symbol[r.symbol].append(r)
    scored = [(sym, symbol_conviction(rs)) for sym, rs in by_symbol.items()]
    scored = [(sym, c) for sym, c in scored if c > 0.0]
    scored.sort(key=lambda x: x[1], reverse=True)
    return [sym for sym, _ in scored[:top_n]]
```

- [ ] **Step 4: Run** `uv run pytest tests/test_screen.py -v` — expect PASS (4 passed). Then `uv run ruff check futures_fund/screen.py tests/test_screen.py`.

- [ ] **Step 5: Commit**

```bash
git add futures_fund/screen.py tests/test_screen.py
git commit -m "feat: analyst-report screen (conviction x agreement -> top-N for debate)"
```

---

## Task 5: Refactor cycle into a reusable `execute_proposals` bridge

**Files:** modify `futures_fund/cycle.py`; create `tests/test_execute_proposals.py`. **The 3 existing `tests/test_cycle.py` tests MUST still pass unchanged.**

This extracts the cycle's data-fetch, exit-audit, and proposal-execution into reusable functions so the Phase-B2 agent cycle can reuse the exact same A1-gate + A3b-execution back half that the baseline cycle uses.

- [ ] **Step 1: Write the failing test** — `tests/test_execute_proposals.py`:

```python
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from futures_fund.config import Settings
from futures_fund.cycle import execute_proposals, fetch_context
from futures_fund.models import TradeProposal
from futures_fund.state import AccountState, load_positions

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
        return FundingInfo(symbol=symbol, current_rate=0.0,
                           next_funding_ts=datetime(2026, 1, 1, tzinfo=UTC), interval_hours=8.0,
                           mark_price=float(self.frames[symbol]["close"].iloc[-1]),
                           index_price=float(self.frames[symbol]["close"].iloc[-1]))


def _uptrend(n=60):
    rng = np.random.default_rng(3)
    close = 100.0 + 0.8 * np.arange(n) + rng.normal(0, 0.05, n)
    return pd.DataFrame({
        "timestamp": pd.date_range("2026-01-01", periods=n, freq="4h", tz="UTC"),
        "open": close, "high": close + 0.2, "low": close - 0.2, "close": close, "volume": 1.0,
    })


def _settings():
    return Settings(account_size_usdt=10_000.0, symbols=["BTC/USDT:USDT"], timeframe="4h")


def test_execute_proposals_opens_and_journals(tmp_path):
    state_dir, memory_dir = tmp_path / "state", tmp_path / "memory"
    from futures_fund.memory_layout import ensure_memory_layout
    ensure_memory_layout(memory_dir)
    ex = FakeExchange({"BTC/USDT:USDT": _uptrend()})
    ctx = fetch_context(ex, _settings())
    last = float(ctx.frames["BTC/USDT:USDT"]["close"].iloc[-1])
    prop = TradeProposal(symbol="BTCUSDT", direction="long", entry=last, stop=last - 4.0,
                         take_profits=[last + 8.0], atr=2.0, confidence=0.7,
                         horizon_hours=4, funding_rate=0.0)
    account = AccountState(balance=10_000.0, peak_equity=10_000.0)
    report = execute_proposals(ctx, [prop], contributing_agents=["research_manager", "trader"],
                               positions=[], account=account, state_dir=state_dir,
                               memory_dir=memory_dir, now=datetime(2026, 3, 1, tzinfo=UTC),
                               cycle_no=1)
    assert report["opened"] == 1
    pos = load_positions(state_dir)
    assert len(pos) == 1 and pos[0].decision_id is not None


def test_execute_proposals_empty_book_opens_nothing(tmp_path):
    state_dir, memory_dir = tmp_path / "state", tmp_path / "memory"
    from futures_fund.memory_layout import ensure_memory_layout
    ensure_memory_layout(memory_dir)
    ex = FakeExchange({"BTC/USDT:USDT": _uptrend()})
    ctx = fetch_context(ex, _settings())
    account = AccountState(balance=10_000.0, peak_equity=10_000.0)
    report = execute_proposals(ctx, [], contributing_agents=["trader"], positions=[],
                               account=account, state_dir=state_dir, memory_dir=memory_dir,
                               now=datetime(2026, 3, 1, tzinfo=UTC), cycle_no=1)
    assert report["opened"] == 0
    assert load_positions(state_dir) == []


def test_baseline_run_cycle_still_works(tmp_path):
    # the refactor must not break the baseline path
    from futures_fund.cycle import run_cycle
    ex = FakeExchange({"BTC/USDT:USDT": _uptrend()})
    report = run_cycle(ex, _settings(), tmp_path / "s", tmp_path / "m",
                       now=datetime(2026, 3, 1, tzinfo=UTC), cycle_no=1)
    assert report["opened"] == 1
```

- [ ] **Step 2: Run** `uv run pytest tests/test_execute_proposals.py -v` — expect FAIL (no `fetch_context`/`execute_proposals`).

- [ ] **Step 3: Refactor `futures_fund/cycle.py`.** Introduce a `CycleContext` dataclass and three functions, then rewrite `run_cycle` to use them. Replace the file's contents with:

```python
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from futures_fund.baseline import propose, simple_regime
from futures_fund.config import Settings
from futures_fund.consolidation import consolidate, cvar_risk_multiplier
from futures_fund.costs import count_funding_events
from futures_fund.executor import close_at_mark, open_position, reconcile
from futures_fund.exits import detect_exit
from futures_fund.hitrate import hit_rate, record_outcome
from futures_fund.journal import append_decision, patch_outcome, read_all_decisions
from futures_fund.liquidation import liquidation_price, mmr_for_notional
from futures_fund.memory_layout import ensure_memory_layout
from futures_fund.models import TradeProposal
from futures_fund.policy import caps_for
from futures_fund.portfolio import portfolio_health
from futures_fund.risk_gate import GateInputs, evaluate
from futures_fund.state import (
    AccountState,
    Position,
    is_halted,
    load_account,
    load_positions,
    save_account,
    save_positions,
)

_BASELINE = "baseline"
_SLIPPAGE_BPS = 2.0


@dataclass
class CycleContext:
    settings: Settings
    frames: dict
    fundings: dict
    specs: dict             # unified symbol -> SymbolSpec
    raw_to_unified: dict    # raw id -> unified symbol
    specs_by_raw: dict      # raw id -> SymbolSpec
    prices: dict            # raw id -> last close


def fetch_context(exchange, settings: Settings) -> CycleContext:
    """Fetch all per-symbol market data once and build the lookup maps the cycle needs."""
    frames = {s: exchange.ohlcv(s, settings.timeframe) for s in settings.symbols}
    fundings = {s: exchange.funding(s) for s in settings.symbols}
    specs = {s: exchange.symbol_spec(s) for s in settings.symbols}
    raw_to_unified = {specs[s].symbol: s for s in settings.symbols}
    specs_by_raw = {specs[s].symbol: specs[s] for s in settings.symbols}
    prices = {specs[s].symbol: float(frames[s]["close"].iloc[-1]) for s in settings.symbols}
    return CycleContext(settings, frames, fundings, specs, raw_to_unified, specs_by_raw, prices)


def _recent_returns(memory_dir, equity: float) -> list[float]:
    pnls = [d["realized_pnl"] for d in read_all_decisions(memory_dir)
            if d.get("realized_pnl") is not None]
    return [p / equity for p in pnls[-30:]] if equity > 0 else []


def audit_and_reflect(ctx: CycleContext, positions: list[Position], account: AccountState,
                      memory_dir, now: datetime, report: dict) -> list[Position]:
    """Phase 1: close positions whose latest bar hit stop/tp/liq; patch outcomes + hit-rate."""
    still_open: list[Position] = []
    for p in positions:
        sym = ctx.raw_to_unified.get(p.symbol)
        if sym is None:
            still_open.append(p)
            report["carried"] += 1
            continue
        bar = ctx.frames[sym].iloc[-1]
        fr = ctx.fundings[sym]
        n_events = count_funding_events(p.opened_ts, now, int(fr.interval_hours))
        ct = detect_exit(p, bar_high=float(bar["high"]), bar_low=float(bar["low"]),
                         funding_rate=fr.current_rate, funding_events=n_events,
                         slippage_bps=_SLIPPAGE_BPS)
        if ct is None:
            still_open.append(p)
            continue
        account.balance += ct.realized_pnl
        report["closed"] += 1
        report["actions"].append({"close": p.symbol, "reason": ct.reason, "pnl": ct.realized_pnl})
        if p.decision_id:
            patch_outcome(memory_dir, p.decision_id, {
                "exit_ts": now, "realized_pnl": ct.realized_pnl, "fees": ct.exit_fee,
                "funding_paid": ct.funding, "slippage": ct.slippage,
                "prediction_correct": ct.realized_pnl > 0,
            })
            record_outcome(memory_dir, _BASELINE, ct.realized_pnl > 0)
    return still_open


def execute_proposals(ctx: CycleContext, proposals: list[TradeProposal], contributing_agents: list[str],
                      positions: list[Position], account: AccountState, state_dir, memory_dir,
                      now: datetime, cycle_no: int, report: dict | None = None) -> dict:
    """Phases 7-10 for a given set of trade proposals (from the baseline OR the agent team):
    risk-gate each proposal, consolidate to a book, reconcile/execute, journal, persist.
    Reusable by both the baseline cycle and the Phase-B agent cycle."""
    if report is None:
        report = {"cycle": cycle_no, "halted": False, "opened": 0, "closed": 0,
                  "carried": 0, "equity": account.balance, "actions": []}
    health = portfolio_health(account.balance, account.peak_equity, positions, ctx.prices,
                              recent_hit_rate=hit_rate(memory_dir, _BASELINE))
    caps = caps_for(simple_regime(ctx.frames[ctx.settings.symbols[0]]), health)
    open_dicts = [{"symbol": p.symbol, "direction": p.direction, "qty": p.qty,
                   "entry": p.entry, "stop": p.stop} for p in positions]

    approved = []
    for prop in proposals:
        spec = ctx.specs_by_raw.get(prop.symbol)
        if spec is None:
            continue
        unified = ctx.raw_to_unified[prop.symbol]
        decision = evaluate(GateInputs(proposal=prop, spec=spec,
                                       regime=simple_regime(ctx.frames[unified]),
                                       health=health, open_positions=open_dicts))
        if decision.verdict in ("approve", "resize") and decision.sized_trade is not None:
            approved.append(decision.sized_trade)

    cvar_mult = cvar_risk_multiplier(_recent_returns(memory_dir, health.equity))
    book = consolidate(approved, health.equity, caps.max_heat, cvar_mult=cvar_mult)

    target = {st.proposal.symbol: st for st in book}
    to_open, to_close = reconcile(target, positions)
    closed_syms: set[str] = set()
    for p in to_close:
        sym = ctx.raw_to_unified.get(p.symbol)
        if sym is None or p.symbol not in ctx.prices:
            continue
        fr = ctx.fundings[sym]
        n_events = count_funding_events(p.opened_ts, now, int(fr.interval_hours))
        ct = close_at_mark(p, ctx.prices[p.symbol], funding_rate=fr.current_rate,
                           funding_events=n_events, slippage_bps=_SLIPPAGE_BPS)
        account.balance += ct.realized_pnl
        report["closed"] += 1
        closed_syms.add(p.symbol)
        report["actions"].append({"close": p.symbol, "reason": "close", "pnl": ct.realized_pnl})
        if p.decision_id:
            patch_outcome(memory_dir, p.decision_id, {
                "exit_ts": now, "realized_pnl": ct.realized_pnl, "fees": ct.exit_fee,
                "funding_paid": ct.funding, "prediction_correct": ct.realized_pnl > 0,
            })
            record_outcome(memory_dir, _BASELINE, ct.realized_pnl > 0)
    keep = [p for p in positions if p.symbol not in closed_syms]
    report["carried"] += sum(1 for p in to_close if p.symbol not in closed_syms)

    for st in to_open:
        spec = ctx.specs_by_raw[st.proposal.symbol]
        did = append_decision(memory_dir, {
            "ts": now, "cycle": cycle_no, "symbol": st.proposal.symbol,
            "direction": st.proposal.direction, "entry": st.proposal.entry,
            "stop": st.proposal.stop, "take_profit": st.proposal.take_profits, "size": st.qty,
            "leverage": st.leverage, "funding_at_entry": st.proposal.funding_rate,
            "confidence": st.proposal.confidence, "dominant_signal": contributing_agents[0]
            if contributing_agents else "unknown", "contributing_agents": contributing_agents,
        })
        pos, entry_fee = open_position(st, cycle_no, now, _SLIPPAGE_BPS, decision_id=did)
        mmr, maint = mmr_for_notional(pos.qty * pos.entry, spec.mmr_brackets)
        liq = liquidation_price(pos.entry, pos.qty, pos.margin, pos.direction, mmr, maint)
        pos = pos.model_copy(update={"liq_price": liq})
        account.balance -= entry_fee
        keep.append(pos)
        report["opened"] += 1
        report["actions"].append({"open": pos.symbol, "direction": pos.direction})

    final_health = portfolio_health(account.balance, account.peak_equity, keep, ctx.prices,
                                    recent_hit_rate=hit_rate(memory_dir, _BASELINE))
    account.peak_equity = max(account.peak_equity, final_health.equity)
    account.updated_ts = now
    save_account(state_dir, account)
    save_positions(state_dir, keep)
    report["equity"] = final_health.equity
    return report


def run_cycle(exchange, settings: Settings, state_dir, memory_dir,
              now: datetime, cycle_no: int) -> dict:
    """Run one deterministic baseline cycle (phases 0-11, no LLM). Returns a CycleReport dict."""
    ensure_memory_layout(memory_dir)
    account = load_account(state_dir, settings.account_size_usdt)
    positions = load_positions(state_dir)
    report = {"cycle": cycle_no, "halted": False, "opened": 0, "closed": 0,
              "carried": 0, "equity": account.balance, "actions": []}
    if is_halted(state_dir):
        report["halted"] = True
        return report

    ctx = fetch_context(exchange, settings)
    positions = audit_and_reflect(ctx, positions, account, memory_dir, now, report)

    proposals = []
    for s in settings.symbols:
        spec = ctx.specs[s]
        prop = propose(spec.symbol, ctx.frames[s], ctx.fundings[s].current_rate, horizon_hours=4.0)
        if prop is not None:
            proposals.append(prop)

    return execute_proposals(ctx, proposals, [_BASELINE], positions, account,
                             state_dir, memory_dir, now, cycle_no, report)
```

- [ ] **Step 4: Run** the cycle + new tests:
  - `uv run pytest tests/test_cycle.py -v` — the 3 ORIGINAL tests must still PASS (the baseline path is unchanged in behavior).
  - `uv run pytest tests/test_execute_proposals.py -v` — expect PASS (3 passed).
  If a `test_cycle.py` test regresses, the refactor changed behavior — fix the refactor (do NOT edit `test_cycle.py`).

- [ ] **Step 5: Run** `uv run ruff check futures_fund/cycle.py tests/test_execute_proposals.py` — fix style only.

- [ ] **Step 6: Commit**

```bash
git add futures_fund/cycle.py tests/test_execute_proposals.py
git commit -m "refactor: extract fetch_context/audit_and_reflect/execute_proposals for agent reuse"
```

---

## Task 6: Reflection glue

**Files:** create `futures_fund/reflect.py`, `tests/test_reflect.py`.

- [ ] **Step 1: Write the failing test** — `tests/test_reflect.py`:

```python
from datetime import datetime, timezone

from futures_fund.journal import append_decision, patch_outcome
from futures_fund.lessons import read_lessons
from futures_fund.memory_layout import ensure_memory_layout
from futures_fund.reflect import record_lesson, reflection_payload

UTC = timezone.utc


def _closed(memory_dir, symbol, pnl):
    did = append_decision(memory_dir, {
        "ts": datetime(2026, 5, 1, tzinfo=UTC), "cycle": 1, "symbol": symbol,
        "direction": "long", "entry": 100.0, "stop": 95.0, "regime": "high_vol_trend",
    })
    patch_outcome(memory_dir, did, {"realized_pnl": pnl, "prediction_correct": pnl > 0})


def test_reflection_payload_splits_winners_and_losers(tmp_path):
    ensure_memory_layout(tmp_path)
    _closed(tmp_path, "BTCUSDT", 50.0)
    _closed(tmp_path, "ETHUSDT", -30.0)
    _closed(tmp_path, "SOLUSDT", 20.0)
    payload = reflection_payload(tmp_path)
    assert len(payload["winners"]) == 2
    assert len(payload["losers"]) == 1
    assert payload["losers"][0]["symbol"] == "ETHUSDT"


def test_record_lesson_appends_candidate_with_provenance(tmp_path):
    ensure_memory_layout(tmp_path)
    lid = record_lesson(tmp_path, text="cut leverage in high-vol chop",
                        regime="high_vol_range", tags=["leverage", "vol"],
                        importance=7, provenance=["dec1", "dec2"],
                        ts=datetime(2026, 5, 2, tzinfo=UTC))
    lessons = read_lessons(tmp_path)
    assert len(lessons) == 1
    lz = lessons[0]
    assert lz.id == lid and lz.state == "candidate"
    assert lz.regime == "high_vol_range" and lz.provenance == ["dec1", "dec2"]
    # also mirrored to the human-readable lessons.md
    md = (tmp_path / "lessons" / "lessons.md").read_text()
    assert "cut leverage in high-vol chop" in md
```

- [ ] **Step 2: Run** `uv run pytest tests/test_reflect.py -v` — expect FAIL.

- [ ] **Step 3: Implement** `futures_fund/reflect.py`:

```python
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from futures_fund.journal import read_all_decisions
from futures_fund.lessons import append_lesson


def reflection_payload(memory_dir) -> dict:
    """Split closed decisions into winners/losers for the Reflector subagent to contrast.
    (The Reflector reasons over this; promotion/validation gating is Phase C.)"""
    closed = [d for d in read_all_decisions(memory_dir) if d.get("realized_pnl") is not None]
    winners = [d for d in closed if d["realized_pnl"] > 0]
    losers = [d for d in closed if d["realized_pnl"] <= 0]
    return {"winners": winners, "losers": losers, "n_closed": len(closed)}


def record_lesson(memory_dir, text: str, regime: str | None, tags: list[str],
                  importance: int, provenance: list[str], ts: datetime) -> str:
    """Persist a Reflector-produced lesson as a CANDIDATE (structured store + human lessons.md)."""
    lid = append_lesson(memory_dir, {
        "text": text, "regime": regime, "tags": tags, "importance": importance,
        "provenance": provenance, "state": "candidate",
    }, ts=ts)
    md = Path(memory_dir) / "lessons" / "lessons.md"
    md.parent.mkdir(parents=True, exist_ok=True)
    with md.open("a") as fh:
        fh.write(f"\n- [CANDIDATE {ts:%Y-%m-%d}] ({regime or 'any'}) {text} "
                 f"<tags: {', '.join(tags)}; from: {', '.join(provenance)}>\n")
    return lid
```

- [ ] **Step 4: Run** `uv run pytest tests/test_reflect.py -v` — expect PASS (2 passed). Then `uv run ruff check futures_fund/reflect.py tests/test_reflect.py`.

- [ ] **Step 5: Run the FULL suite + lint:** `uv run pytest` then `uv run ruff check .`. Report the EXACT total (expected 133 + contracts 5 + lessons 5 + brief 2 + screen 4 + execute_proposals 3 + reflect 2 = **154**).

- [ ] **Step 6: Commit**

```bash
git add futures_fund/reflect.py tests/test_reflect.py
git commit -m "feat: reflection payload builder + CANDIDATE lesson recorder"
```

---

## Self-Review (completed during planning)

**Spec coverage (§3.1 funnel, §6 memory):** structured agent contracts incl. 5-tier rating + falsifiable prediction ✓ (T1); lesson store + regime-filtered recency·importance·relevance retrieval (top-K) ✓ (T2); per-symbol analyst brief ✓ (T3); analyst→screen funnel (conviction×agreement → top-N) ✓ (T4); the agent→execution bridge reusing A1 gate + A3b execution ✓ (T5); reflection payload + CANDIDATE lesson recording ✓ (T6). Deferred to B2 (correct): the actual `MISSION.md`/`SKILL.md`/`agents/*.md` that produce these contracts via subagent dispatch; the bull/bear debate transcript handling (the orchestrator runs rounds and feeds the Research Manager — a markdown/orchestration concern, not a Python rail). Deferred to C: lesson promotion CANDIDATE→VALIDATED + the walk-forward/DSR gate.

**Placeholder scan:** none — every step has runnable code/fixtures and exact commands.

**Type/interface consistency:** `to_trade_proposal` returns A1 `TradeProposal` (verified field names) consumed by `execute_proposals` → A1 `evaluate`. `AgentProposal.symbol`/`SymbolSpec.symbol` are both raw ids; `Candidate.symbol` is unified. `retrieve_lessons`/`score_lesson`/`Lesson` consistent. The cycle refactor preserves `run_cycle`'s signature and behavior (guarded by the unchanged `test_cycle.py`), and `execute_proposals`/`fetch_context`/`CycleContext` are the new reuse seams B2 will call. `record_lesson`/`append_lesson` field names match the `Lesson` model.

**Refactor risk (T5):** `run_cycle` is rewritten to delegate to `fetch_context`/`audit_and_reflect`/`execute_proposals`. The behavior is identical (same phase order, same math), so the 3 `test_cycle.py` tests are the regression guard — Step 4 runs them explicitly. `execute_proposals` defaults `report` so it can be called standalone (tests) or threaded from `run_cycle`.

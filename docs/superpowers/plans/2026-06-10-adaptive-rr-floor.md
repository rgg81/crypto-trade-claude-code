# Adaptive RR Floor — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the gate's fixed `MIN_RR=2.0` reward:risk veto with a per-regime-quadrant floor adapting in `[1.6, 2.5]`, seeded at 2.0 and tuned automatically by the reflection loop from multi-bar first-touch scoring of RR-vetoed shadow trades.

**Architecture:** A new pure module `rr_floor.py` owns the floor state + adaptation math; `shadow.py` gains multi-bar first-touch scoring + a resolution cache; the protected `risk_gate` gains an optional per-trade `rr_floor` (wrapped by a gate-owned `HARD_MIN_RR=1.6`); the protected `cycle.execute_proposals` loads the floor state and threads the per-quadrant floor into each `GateInputs` (and stamps `quadrant`+`id` on vetoed shadow entries); `orchestration` switches the CP9 arm-guard to the same effective floor and, at the end of the gate step (where OHLCV frames are available), scores resolvable shadow entries and writes the adapted `rr_floor.json` for next cycle.

**Tech Stack:** Python 3.11, pydantic, pandas, pytest, ruff, `uv`. Spec: `docs/superpowers/specs/2026-06-10-adaptive-rr-floor-design.md`.

**Conventions:** run tests with `uv run pytest`; lint with `uv run ruff check <files>`. Keep lines ≤100 chars (E501). Protected modules (`risk_gate`, `cycle`) are edited ONLY as specified here under explicit user authorization; the edits must not let any veto drop below the gate-owned `HARD_MIN_RR`.

**File structure:**
- Create `futures_fund/rr_floor.py` — pure: constants, `load_rr_floor`, `save_rr_floor`, `effective_rr_floor`, `adapt_rr_floor`.
- Modify `futures_fund/shadow.py` — add `score_shadow_first_touch`, `tally_resolutions`, `load_scored`, `save_scored`.
- Modify `futures_fund/risk_gate.py` (PROTECTED) — `GateInputs.rr_floor`, `HARD_MIN_RR`, floor-wrap in `evaluate`.
- Modify `futures_fund/cycle.py` (PROTECTED) — `execute_proposals`: load floor, thread per-quadrant floor, stamp `quadrant`+`id` on vetoed entries.
- Modify `futures_fund/orchestration.py` — CP9 arm-guard uses effective floor; end-of-gate scoring+adaptation+warnings.
- Tests: `tests/test_rr_floor.py` (new), `tests/test_shadow.py` (extend or new), `tests/test_risk_gate.py`, `tests/test_gate_wiring.py`.

---

### Task 1: `rr_floor.py` — state load + effective floor (clamp + fail-safe)

**Files:**
- Create: `futures_fund/rr_floor.py`
- Test: `tests/test_rr_floor.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_rr_floor.py
import json
from futures_fund.rr_floor import (
    BAND, SEED, QUADRANTS, load_rr_floor, save_rr_floor, effective_rr_floor,
)


def test_seed_and_band_constants():
    assert SEED == 2.0 and BAND == (1.6, 2.5)
    assert set(QUADRANTS) == {"high_vol_trend", "low_vol_trend", "high_vol_range", "low_vol_range"}


def test_load_missing_returns_all_seed(tmp_path):
    state = load_rr_floor(tmp_path)
    assert all(state[q] == SEED for q in QUADRANTS) and state["updated_cycle"] == 0


def test_load_corrupt_or_partial_fails_safe(tmp_path):
    (tmp_path / "rr_floor.json").write_text("{ not json")
    assert all(load_rr_floor(tmp_path)[q] == SEED for q in QUADRANTS)
    (tmp_path / "rr_floor.json").write_text(json.dumps({"low_vol_range": 1.7}))
    s = load_rr_floor(tmp_path)
    assert s["low_vol_range"] == 1.7 and s["high_vol_trend"] == SEED   # missing keys -> SEED


def test_effective_clamps_to_band():
    assert effective_rr_floor("low_vol_range", {"low_vol_range": 1.4}) == 1.6   # below band
    assert effective_rr_floor("high_vol_trend", {"high_vol_trend": 3.0}) == 2.5  # above band
    assert effective_rr_floor("low_vol_trend", {"low_vol_trend": 1.9}) == 1.9    # in band
    assert effective_rr_floor("missing_q", {}) == SEED                          # unknown -> SEED


def test_save_then_load_roundtrip(tmp_path):
    save_rr_floor(tmp_path, {"high_vol_trend": 2.1, "low_vol_trend": 2.0,
                             "high_vol_range": 1.8, "low_vol_range": 1.7, "updated_cycle": 5})
    s = load_rr_floor(tmp_path)
    assert s["low_vol_range"] == 1.7 and s["updated_cycle"] == 5
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_rr_floor.py -q`
Expected: FAIL — `ModuleNotFoundError: futures_fund.rr_floor`.

- [ ] **Step 3: Implement**

```python
# futures_fund/rr_floor.py
from __future__ import annotations

import json
import math
from pathlib import Path

QUADRANTS = ("high_vol_trend", "low_vol_trend", "high_vol_range", "low_vol_range")
SEED = 2.0
BAND = (1.6, 2.5)            # (hard floor, ceiling) — the adaptive RR floor never leaves this band
LOOSEN_STEP = 0.05          # slow to relax the limit
TIGHTEN_STEP = 0.10         # fast to re-tighten (safety-asymmetric)
N_MIN = 8                   # min decided (won+lost) samples before a quadrant floor moves
WIN_HI = 0.60               # would-have-won rate above this -> vetoes cost winners -> loosen
WIN_LO = 0.40               # below this -> vetoes save losers -> tighten


def _path(state_dir) -> Path:
    return Path(state_dir) / "rr_floor.json"


def clamp(x: float) -> float:
    lo, hi = BAND
    return max(lo, min(hi, x))


def load_rr_floor(state_dir) -> dict:
    """All four quadrant floors + updated_cycle. FAIL-SAFE: missing file / corrupt JSON / missing
    key -> SEED (today's 2.0 behaviour), never raises."""
    out = {q: SEED for q in QUADRANTS}
    out["updated_cycle"] = 0
    try:
        raw = json.loads(_path(state_dir).read_text())
    except (OSError, ValueError):
        return out
    if not isinstance(raw, dict):
        return out
    for q in QUADRANTS:
        v = raw.get(q)
        if isinstance(v, (int, float)) and math.isfinite(v):
            out[q] = float(v)
    uc = raw.get("updated_cycle")
    if isinstance(uc, int):
        out["updated_cycle"] = uc
    return out


def save_rr_floor(state_dir, state: dict) -> None:
    p = _path(state_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, indent=2))


def effective_rr_floor(quadrant: str, state: dict) -> float:
    """The floor a trade in `quadrant` is judged on, clamped to BAND. Unknown quadrant -> SEED."""
    raw = state.get(quadrant, SEED)
    if not isinstance(raw, (int, float)) or not math.isfinite(raw):
        raw = SEED
    return clamp(float(raw))
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_rr_floor.py -q`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add futures_fund/rr_floor.py tests/test_rr_floor.py
git commit -m "feat(rr_floor): per-quadrant floor state load/save + clamped effective floor"
```

---

### Task 2: `rr_floor.py` — `adapt_rr_floor` (asymmetric step, dead-band, min-samples)

**Files:**
- Modify: `futures_fund/rr_floor.py`
- Test: `tests/test_rr_floor.py`

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_rr_floor.py
from futures_fund.rr_floor import adapt_rr_floor, SEED


def _seed_state():
    return {q: SEED for q in ("high_vol_trend", "low_vol_trend", "high_vol_range", "low_vol_range")} \
        | {"updated_cycle": 0}


def test_adapt_loosens_when_vetoes_cost_winners():
    # low_vol_range: 7 won / 1 lost (w=0.875 > 0.60) and >=8 decided -> loosen -0.05
    new, changes = adapt_rr_floor(_seed_state(), {"low_vol_range": (7, 1)}, cycle_no=10)
    assert new["low_vol_range"] == 1.95 and new["updated_cycle"] == 10
    assert any("low_vol_range" in c and "2.0" in c for c in changes)


def test_adapt_tightens_when_vetoes_save_losers():
    # 2 won / 8 lost (w=0.2 < 0.40) -> tighten +0.10, but SEED 2.0 is already clamped at ceiling? No:
    # tightening pushes toward 2.5. 2.0 -> 2.10
    new, _ = adapt_rr_floor(_seed_state(), {"high_vol_trend": (2, 8)}, cycle_no=11)
    assert new["high_vol_trend"] == 2.10


def test_adapt_deadband_no_change():
    new, changes = adapt_rr_floor(_seed_state(), {"low_vol_trend": (5, 5)}, cycle_no=12)  # w=0.5
    assert new["low_vol_trend"] == SEED and changes == []


def test_adapt_requires_min_samples():
    new, changes = adapt_rr_floor(_seed_state(), {"low_vol_range": (7, 0)}, cycle_no=13)  # only 7
    assert new["low_vol_range"] == SEED and changes == []


def test_adapt_clamps_at_bounds():
    st = _seed_state() | {"low_vol_range": 1.6}
    new, _ = adapt_rr_floor(st, {"low_vol_range": (8, 0)}, cycle_no=14)   # would go 1.55 -> clamp 1.6
    assert new["low_vol_range"] == 1.6
    st2 = _seed_state() | {"high_vol_trend": 2.5}
    new2, _ = adapt_rr_floor(st2, {"high_vol_trend": (0, 8)}, cycle_no=15)  # would go 2.6 -> clamp 2.5
    assert new2["high_vol_trend"] == 2.5
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_rr_floor.py -q -k adapt`
Expected: FAIL — `ImportError: cannot import name 'adapt_rr_floor'`.

- [ ] **Step 3: Implement (append to `futures_fund/rr_floor.py`)**

```python
def adapt_rr_floor(state: dict, won_lost_by_quadrant: dict, cycle_no: int) -> tuple[dict, list]:
    """Nudge each quadrant's floor from its trailing-window (won, lost) tally. Safety-asymmetric:
    w>WIN_HI (vetoes cost winners) -> loosen by LOOSEN_STEP; w<WIN_LO (vetoes save losers) ->
    tighten by TIGHTEN_STEP; dead-band between = no change. Needs >= N_MIN decided samples. Pure;
    returns (new_state, [human-readable change strings]). Always clamps to BAND."""
    new = dict(state)
    changes: list = []
    for q in QUADRANTS:
        won, lost = won_lost_by_quadrant.get(q, (0, 0))
        decided = won + lost
        if decided < N_MIN:
            continue
        w = won / decided
        cur = clamp(float(state.get(q, SEED)))
        if w > WIN_HI:
            nxt = clamp(cur - LOOSEN_STEP)
        elif w < WIN_LO:
            nxt = clamp(cur + TIGHTEN_STEP)
        else:
            continue
        if nxt != cur:
            new[q] = nxt
            changes.append(f"rr_floor {q} {cur:.2f}->{nxt:.2f} "
                           f"(vetoes {'cost' if w > WIN_HI else 'saved'} {won}/{decided}, w={w:.2f})")
    if changes:
        new["updated_cycle"] = cycle_no
    return new, changes
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_rr_floor.py -q`
Expected: PASS (10 tests).

- [ ] **Step 5: Commit**

```bash
git add futures_fund/rr_floor.py tests/test_rr_floor.py
git commit -m "feat(rr_floor): safety-asymmetric adapt_rr_floor (loosen/tighten, dead-band, min-samples)"
```

---

### Task 3: `shadow.py` — multi-bar first-touch scoring + resolution cache

**Files:**
- Modify: `futures_fund/shadow.py`
- Test: `tests/test_shadow.py` (create if absent)

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_shadow.py
import json
from futures_fund.shadow import (
    score_shadow_first_touch, tally_resolutions, load_scored, save_scored, HORIZON,
)


def _short(entry=100.0, stop=104.0, tp=88.0):   # short: tp below, stop above; risk 4, reward 12
    return {"direction": "short", "entry": entry, "stop": stop, "take_profits": [tp]}


def test_first_touch_won_short():
    bars = [{"high": 101, "low": 99}, {"high": 100, "low": 87}]   # 2nd bar hits tp 88
    assert score_shadow_first_touch(_short(), bars) == "won"


def test_first_touch_lost_short():
    bars = [{"high": 105, "low": 100}]    # high 105 >= stop 104 first
    assert score_shadow_first_touch(_short(), bars) == "lost"


def test_same_bar_tp_and_stop_resolves_lost():
    bars = [{"high": 105, "low": 87}]     # both touched same bar -> conservative LOST
    assert score_shadow_first_touch(_short(), bars) == "lost"


def test_pending_then_expired():
    assert score_shadow_first_touch(_short(), [{"high": 101, "low": 95}]) == "pending"
    flat = [{"high": 101, "low": 95}] * HORIZON
    assert score_shadow_first_touch(_short(), flat) == "expired"


def test_long_mirror():
    long = {"direction": "long", "entry": 100.0, "stop": 96.0, "take_profits": [112.0]}
    assert score_shadow_first_touch(long, [{"high": 112, "low": 99}]) == "won"
    assert score_shadow_first_touch(long, [{"high": 101, "low": 95}]) == "lost"


def test_tally_resolutions_trailing_window():
    scored = {
        "a": {"outcome": "won", "quadrant": "low_vol_range"},
        "b": {"outcome": "lost", "quadrant": "low_vol_range"},
        "c": {"outcome": "expired", "quadrant": "low_vol_range"},   # excluded
        "d": {"outcome": "won", "quadrant": "high_vol_trend"},
        "e": {"outcome": "pending", "quadrant": "low_vol_range"},   # excluded
    }
    t = tally_resolutions(scored, trail_w=40)
    assert t["low_vol_range"] == (1, 1) and t["high_vol_trend"] == (1, 0)


def test_scored_cache_roundtrip(tmp_path):
    save_scored(tmp_path, {"x": {"outcome": "won", "quadrant": "low_vol_range", "cycle": 3}})
    assert load_scored(tmp_path)["x"]["outcome"] == "won"
    assert load_scored(tmp_path / "nope") == {}     # missing -> empty
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_shadow.py -q`
Expected: FAIL — `ImportError: cannot import name 'score_shadow_first_touch'`.

- [ ] **Step 3: Implement (append to `futures_fund/shadow.py`)**

```python
HORIZON = 12   # bars (~2 days at 4h) a vetoed trade is tracked before it 'expires' undecided


def score_shadow_first_touch(entry: dict, bars: list[dict]) -> str:
    """First-touch outcome of a would-be (vetoed) trade over up to HORIZON bars AFTER the veto.
    `bars` is chronological dicts with 'high'/'low'. Returns 'won' (TP touched before stop), 'lost'
    (stop touched first; a same-bar TP+stop ambiguity resolves conservatively to 'lost'), 'pending'
    (< HORIZON bars seen, neither touched), or 'expired' (>= HORIZON bars, neither touched)."""
    e, stop = entry["entry"], entry["stop"]
    tps = entry.get("take_profits") or []
    tp = tps[0] if tps else None
    is_long = entry["direction"] == "long"
    for bar in bars[:HORIZON]:
        hi, lo = bar.get("high"), bar.get("low")
        if hi is None or lo is None:
            continue
        if is_long:
            stop_hit = lo <= stop
            tp_hit = tp is not None and hi >= tp
        else:
            stop_hit = hi >= stop
            tp_hit = tp is not None and lo <= tp
        if stop_hit:                      # conservative: stop wins a same-bar tie
            return "lost"
        if tp_hit:
            return "won"
    return "expired" if len(bars) >= HORIZON else "pending"


def tally_resolutions(scored: dict, trail_w: int) -> dict:
    """Per-quadrant (won, lost) counts over the most recent `trail_w` DECIDED (won/lost) entries.
    `scored` maps id -> {outcome, quadrant, ...}. 'pending'/'expired' are excluded. Insertion order
    of `scored` is the chronological order (entries appended as resolved)."""
    by_q: dict = {}
    decided = [v for v in scored.values() if v.get("outcome") in ("won", "lost")]
    for v in decided[-trail_w:]:
        q = v.get("quadrant")
        if q is None:
            continue
        won, lost = by_q.get(q, (0, 0))
        by_q[q] = (won + (v["outcome"] == "won"), lost + (v["outcome"] == "lost"))
    return by_q


def _scored_path(state_dir):
    return Path(state_dir) / "shadow-scored.json"


def load_scored(state_dir) -> dict:
    try:
        return json.loads(_scored_path(state_dir).read_text())
    except (OSError, ValueError):
        return {}


def save_scored(state_dir, scored: dict) -> None:
    p = _scored_path(state_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(scored))
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_shadow.py -q`
Expected: PASS (7 tests).

- [ ] **Step 5: Commit**

```bash
git add futures_fund/shadow.py tests/test_shadow.py
git commit -m "feat(shadow): multi-bar first-touch scoring + trailing tally + resolution cache"
```

---

### Task 4: `risk_gate.py` (PROTECTED) — `GateInputs.rr_floor` + `HARD_MIN_RR` wrap

**Files:**
- Modify: `futures_fund/risk_gate.py:20-21` (constants), `GateInputs` (~:25), `evaluate` RR check (~:76-79)
- Test: `tests/test_risk_gate.py`

- [ ] **Step 1: Write the failing tests** (append to `tests/test_risk_gate.py`; reuse that file's existing `GateInputs`/`evaluate`/proposal/spec builders — match its helper names)

```python
# tests/test_risk_gate.py — adapt builders to the file's existing helpers (e.g. _proposal/_spec/_inputs)
from futures_fund.risk_gate import HARD_MIN_RR, MIN_RR


def test_hard_min_rr_constant():
    assert HARD_MIN_RR == 1.6 and MIN_RR == 2.0


def test_gate_uses_adaptive_floor_below_default():
    # a 1.7-RR proposal: vetoed at default (None->2.0), approved when rr_floor=1.6
    p = _proposal_rr(1.7)                       # helper: build a proposal whose _reward_risk == 1.7
    assert evaluate(_inputs(p, rr_floor=None)).verdict == "veto"
    assert evaluate(_inputs(p, rr_floor=1.6)).verdict in ("approve", "resize")


def test_gate_hard_min_wraps_corrupt_floor():
    # a hostile rr_floor below the hard min cannot admit a 1.5-RR trade
    p = _proposal_rr(1.5)
    assert evaluate(_inputs(p, rr_floor=0.5)).verdict == "veto"   # wrapped up to 1.6
    assert "RR" in evaluate(_inputs(p, rr_floor=0.5)).reason
```

(If `tests/test_risk_gate.py` lacks `_proposal_rr`/`_inputs` helpers, add small ones that construct a `TradeProposal` with entry/stop/take_profits giving the target RR and a `GateInputs` with a benign regime/health so only the RR check is exercised. Look at the file's existing tests for the exact `TradeProposal`/`GateInputs`/`SymbolSpec`/`PortfolioHealth` construction and copy it.)

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_risk_gate.py -q -k "hard_min or adaptive_floor"`
Expected: FAIL — `ImportError: cannot import name 'HARD_MIN_RR'`.

- [ ] **Step 3: Implement**

In `futures_fund/risk_gate.py`, after `MIN_RR = 2.0` / `_RR_EPS = 1e-6` (line ~20-21) add:

```python
HARD_MIN_RR = 1.6   # gate-owned ABSOLUTE floor: an adaptive rr_floor can never drop a veto below this
```

In `GateInputs(BaseModel)` add the field (after `monthly_pnl_pct`):

```python
    rr_floor: float | None = None   # regime-adaptive RR floor; None -> MIN_RR. Wrapped by HARD_MIN_RR.
```

Replace the RR check (lines ~76-79):

```python
    # 2. Reward:risk — regime-adaptive floor, but never below the gate-owned HARD_MIN_RR.
    rr = _reward_risk(p)
    floor = max(inp.rr_floor if inp.rr_floor is not None else MIN_RR, HARD_MIN_RR)
    if rr < floor - _RR_EPS:
        return RiskDecision(verdict="veto", reason=f"RR {rr:.2f} < min {floor:.2f}")
```

(Use whatever the function's `GateInputs` param is named — the file uses `inp` at line 72; match it.)

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_risk_gate.py -q`
Expected: PASS (all, including the existing RR tests — the default `rr_floor=None` preserves 2.0).

- [ ] **Step 5: Commit**

```bash
git add futures_fund/risk_gate.py tests/test_risk_gate.py
git commit -m "feat(risk_gate): optional adaptive rr_floor wrapped by gate-owned HARD_MIN_RR=1.6"
```

---

### Task 5: `cycle.py` (PROTECTED) — load + thread floor, stamp `quadrant`+`id` on vetoed entries

**Files:**
- Modify: `futures_fund/cycle.py:160-175` (the proposal loop)
- Test: `tests/test_gate_wiring.py` (integration via `gate_execute_step`)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_gate_wiring.py — a low-vol proposal at RR ~1.7 is vetoed at the seed floor,
# and the veto's shadow entry carries quadrant + id.
def test_vetoed_entry_carries_quadrant_and_id(tmp_path):
    from futures_fund.shadow import shadow_ledger
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    ex = FakeExchange({"BTC/USDT:USDT": _uptrend()})
    _pf(state_dir, memory_dir, ex)
    last = _pf(state_dir, memory_dir, ex)["briefs"][0]["last_close"]
    # a market proposal whose RR ~1.7 (below seed 2.0) -> vetoed
    props = [{"symbol": "BTCUSDT", "direction": "long", "entry": last,
              "stop": last - 10.0, "take_profits": [last + 17.0], "atr": 2.0}]
    gate_execute_step(ex, _settings(), state_dir, memory_dir, now=NOW, cycle_no=1,
                      proposals=props, regime_state=_regime("risk_off"))
    led = [e for e in shadow_ledger(state_dir) if e.get("reason", "").startswith("RR")]
    assert led and "quadrant" in led[0] and "id" in led[0]
```

(Match the existing `gate_execute_step` proposal-input shape in `tests/test_gate_wiring.py`; if market proposals route differently, build the proposal the same way an existing "gate vetoes a low-RR proposal" test does. If none exists, the integration assertion can instead drive `execute_proposals` directly with a `CycleContext` like `tests/test_cycle.py` does.)

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_gate_wiring.py -q -k vetoed_entry_carries`
Expected: FAIL — KeyError/assert: entry has no `quadrant`/`id`.

- [ ] **Step 3: Implement** — edit the loop in `futures_fund/cycle.py` (lines ~158-175).

Add imports at top of `cycle.py` (near the other `futures_fund` imports):

```python
from futures_fund.rr_floor import effective_rr_floor, load_rr_floor
```

Before the `for prop in proposals:` loop (after `vetoed: list = []`), load the floor once:

```python
    floor_state = load_rr_floor(state_dir)
```

Replace the loop body (lines ~160-175) with:

```python
    for prop in proposals:
        spec = ctx.specs_by_raw.get(prop.symbol)
        if spec is None:
            continue
        unified = ctx.raw_to_unified[prop.symbol]
        regime = simple_regime(ctx.frames[unified])
        rr_floor = effective_rr_floor(regime.quadrant, floor_state)
        decision = evaluate(GateInputs(proposal=prop, spec=spec, regime=regime,
                                       health=health, open_positions=open_dicts,
                                       daily_pnl_pct=daily_pnl, weekly_pnl_pct=weekly_pnl,
                                       monthly_pnl_pct=monthly_pnl, rr_floor=rr_floor))
        if decision.verdict in ("approve", "resize") and decision.sized_trade is not None:
            approved.append(decision.sized_trade)
        else:
            vetoed.append({"symbol": prop.symbol, "direction": prop.direction,
                           "entry": prop.entry, "stop": prop.stop,
                           "take_profits": prop.take_profits, "reason": decision.reason,
                           "quadrant": regime.quadrant,
                           "id": f"{cycle_no}:{prop.symbol}:{prop.direction}"})
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_gate_wiring.py tests/test_cycle.py -q`
Expected: PASS (new test green; existing cycle tests unaffected — the seed floor == old 2.0).

- [ ] **Step 5: Commit**

```bash
git add futures_fund/cycle.py tests/test_gate_wiring.py
git commit -m "feat(cycle): thread per-quadrant rr_floor into the gate; stamp quadrant+id on vetoes"
```

---

### Task 6: `orchestration.py` — CP9 arm-guard uses the effective floor (not the constant)

**Files:**
- Modify: `futures_fund/orchestration.py` (the CP9 block ~:913-928 and the `swings`/regime context already built in the gate step)
- Test: `tests/test_gate_wiring.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_gate_wiring.py — with low_vol_range floor learned to 1.6, a RR-1.7 stop_entry in that
# regime is ARMED (was refused at the 2.0 constant).
def test_cp9_uses_adaptive_floor(tmp_path):
    from futures_fund.rr_floor import save_rr_floor
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    ex = FakeExchange({"BTC/USDT:USDT": _range_low_vol()})   # a fixture whose simple_regime is low_vol_range
    last = _pf(state_dir, memory_dir, ex)["briefs"][0]["last_close"]
    save_rr_floor(state_dir, {"high_vol_trend": 2.0, "low_vol_trend": 2.0,
                              "high_vol_range": 2.0, "low_vol_range": 1.6, "updated_cycle": 1})
    report = gate_execute_step(
        ex, _settings(), state_dir, memory_dir, now=NOW, cycle_no=2, proposals=[],
        regime_state=_regime("risk_off"),
        triggers=[{"symbol": "BTCUSDT", "direction": "short", "kind": "stop_entry",
                   "trigger_level": last - 1.0, "stop": last + 0.7,   # RR ~1.7
                   "take_profits": [last - 5.1], "atr": 1.0}])
    assert report["triggers_refused_low_rr"] == 0 and report["triggers_armed"] == 1
```

(Add a `_range_low_vol()` fixture — a flat, low-volatility frame so `simple_regime(...).quadrant == "low_vol_range"`. Verify the quadrant in a quick REPL/`python -c` while writing. Pick `trigger_level`/`stop`/`take_profits` so `trigger_rr` is ~1.7 — between 1.6 and 2.0 — so the result flips on the floor.)

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_gate_wiring.py -q -k cp9_uses_adaptive`
Expected: FAIL — `triggers_refused_low_rr == 1` (CP9 still uses the 2.0 constant).

- [ ] **Step 3: Implement** — in `futures_fund/orchestration.py`.

Add to the pending_orders import block (where `low_rr_triggers`/`trigger_rr` are imported):

```python
    from futures_fund.rr_floor import effective_rr_floor, load_rr_floor
```

The gate step already builds `swings_by_symbol` from per-symbol completed frames; it also builds `bars_by_symbol`. To get each trigger's quadrant, compute a per-symbol regime quadrant alongside `swings_by_symbol`. Where `swings_by_symbol[raw] = (float(sh), float(sl))` is set (the loop over `ctx.raw_to_unified`), also capture:

```python
        try:
            from futures_fund.baseline import simple_regime
            quadrant_by_symbol[raw] = simple_regime(df).quadrant
        except Exception:  # noqa: BLE001 — feed gap -> no quadrant -> floor defaults to SEED below
            pass
```

(Initialize `quadrant_by_symbol: dict = {}` next to `swings_by_symbol: dict = {}`.)

Replace the CP9 block (currently importing `MIN_RR`/`_RR_EPS` and calling `low_rr_triggers(new_triggers, MIN_RR, _RR_EPS)`) so each trigger is checked against its own regime floor. Since `low_rr_triggers` takes a single `min_rr`, partition per trigger here instead:

```python
    # CP9 ARM-TIME RR-FLOOR guard (regime-adaptive): refuse a stop_entry whose RR is below the gate's
    # effective floor for that symbol's regime quadrant. entry==trigger so the RR is fixed arm->fire;
    # a sub-floor trigger would only fire then RR-veto (cy68) = a wasted arm. Refuse-only, symmetric.
    if new_triggers:
        from futures_fund.risk_gate import _RR_EPS
        floor_state = load_rr_floor(state_dir)
        kept_rr, low_rr = [], []
        for o in new_triggers:
            q = quadrant_by_symbol.get(o.symbol)
            floor = effective_rr_floor(q, floor_state) if q else MIN_RR
            checkable = (o.kind == "stop_entry" and o.trigger_level is not None
                         and o.stop is not None and bool(o.take_profits))
            (low_rr if (checkable and trigger_rr(o) < floor - _RR_EPS) else kept_rr).append(o)
        if low_rr:
            new_triggers = kept_rr
            triggers_refused_low_rr += len(low_rr)
            for o in low_rr:
                q = quadrant_by_symbol.get(o.symbol)
                floor = effective_rr_floor(q, floor_state) if q else MIN_RR
                low_rr_actions.append(
                    f"refused LOW-RR arm {o.direction} {o.kind} {o.symbol} @ {o.trigger_level} — "
                    f"RR {trigger_rr(o):.2f} < floor {floor:.2f}; re-spec or stand aside")
```

(`MIN_RR` is still imported in this function from the prior CP9 import; keep that import. `low_rr_triggers` remains in `pending_orders` for its unit tests — it is simply no longer the call site here.)

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_gate_wiring.py -q`
Expected: PASS — including the existing `test_gate_refuses_sub_rr_trigger_at_arm_keeps_clean_one` (no `rr_floor.json` -> SEED 2.0 -> identical behaviour).

- [ ] **Step 5: Commit**

```bash
git add futures_fund/orchestration.py tests/test_gate_wiring.py
git commit -m "feat(gate): CP9 arm-guard uses the regime-adaptive RR floor (per-trigger quadrant)"
```

---

### Task 7: `orchestration.py` — end-of-gate scoring + adaptation + surfaced warnings

**Files:**
- Modify: `futures_fund/orchestration.py` (end of `gate_execute_step`, after the trigger report fields are set)
- Test: `tests/test_gate_wiring.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_gate_wiring.py — given a shadow ledger of past RR-vetoes in low_vol_range that all WON
# (scored from this cycle's frames), the floor for that quadrant loosens and a warning is surfaced.
def test_reflect_adapts_floor_from_shadow_wins(tmp_path):
    from futures_fund.shadow import record_shadow
    from futures_fund.rr_floor import load_rr_floor
    import datetime as _dt
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    ex = FakeExchange({"BTC/USDT:USDT": _downtrend_then_drop()})  # frames where a short hits its TP
    _pf(state_dir, memory_dir, ex)
    # 8 past RR-vetoed shorts in low_vol_range, all structured to WIN on the frame's forward bars
    entries = [{"symbol": "BTCUSDT", "direction": "short", "entry": E, "stop": E + 1.0,
                "take_profits": [E - 5.0], "reason": "RR 1.70 < min 2.00",
                "quadrant": "low_vol_range", "id": f"0:{i}"} for i, E in enumerate([...8 levels...])]
    record_shadow(state_dir, _dt.datetime(2026, 6, 9, tzinfo=_dt.timezone.utc), 0, entries)
    report = gate_execute_step(ex, _settings(), state_dir, memory_dir, now=NOW, cycle_no=5,
                               proposals=[], regime_state=_regime("risk_off"))
    assert load_rr_floor(state_dir)["low_vol_range"] < 2.0
    assert any("rr_floor low_vol_range" in a for a in report.get("warnings", []))
```

(This test is the hardest to fixture: the 8 shadow entries' TP must be reachable in the BTC frame's bars so first-touch scores `won`. Simplest: build the frame and pick entry/stop/tp levels off its actual bars in a `python -c` probe while writing. If full frame-driven scoring is too fiddly to fixture, split: unit-test the scoring glue with an injected `frames_by_symbol` dict of bar lists rather than a live frame — see the implementation's `_resolve_shadow` which should accept a `bars_for(symbol) -> list[dict]` lookup so it is unit-testable without an exchange.)

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_gate_wiring.py -q -k reflect_adapts`
Expected: FAIL — floor unchanged at 2.0, no `rr_floor` warning.

- [ ] **Step 3: Implement** — add a helper + call it at the end of `gate_execute_step`.

Add a module-level helper in `orchestration.py` (pure-ish; takes a `bars_for` lookup so it is testable):

```python
def _resolve_and_adapt_rr_floor(state_dir, bars_for, cycle_no):
    """Score newly-resolvable RR-vetoed shadow entries (first-touch over the symbol's forward bars
    from `bars_for(symbol)`), persist the resolution cache, then adapt the per-quadrant RR floor from
    the trailing tally. Returns the list of human-readable floor-change strings (possibly empty).
    Pure side effects: writes state/shadow-scored.json and state/rr_floor.json."""
    from futures_fund.rr_floor import adapt_rr_floor, load_rr_floor, save_rr_floor
    from futures_fund.shadow import (
        load_scored, save_scored, score_shadow_first_touch, shadow_ledger, tally_resolutions,
    )
    scored = load_scored(state_dir)
    for e in shadow_ledger(state_dir):
        eid = e.get("id")
        if not eid or not str(e.get("reason", "")).startswith("RR") or e.get("quadrant") is None:
            continue
        if scored.get(eid, {}).get("outcome") in ("won", "lost", "expired"):
            continue   # terminal -> done
        bars = bars_for(e["symbol"])
        if not bars:
            continue   # symbol absent this cycle -> retry later
        outcome = score_shadow_first_touch(e, bars)
        if outcome == "pending":
            continue
        scored[eid] = {"outcome": outcome, "quadrant": e["quadrant"], "cycle": cycle_no}
    save_scored(state_dir, scored)
    new_state, changes = adapt_rr_floor(load_rr_floor(state_dir),
                                        tally_resolutions(scored, trail_w=40), cycle_no)
    if changes:
        save_rr_floor(state_dir, new_state)
    return changes
```

At the END of `gate_execute_step` (after `report["triggers_refused_low_rr"] = ...` and its warnings), build a `bars_for` over the frames already loaded this cycle and call the helper:

```python
    def _bars_for(symbol):
        uni = ctx.raw_to_unified.get(symbol)
        df = last_completed_frame(ctx.frames.get(uni), now, settings.timeframe) if uni else None
        if df is None or not len(df):
            return []
        # bars AFTER each veto are matched by first-touch scan; pass the most recent HORIZON bars
        tail = df.tail(24)
        return [{"high": float(r.high), "low": float(r.low)} for r in tail.itertuples()]
    rr_changes = _resolve_and_adapt_rr_floor(state_dir, _bars_for, cycle_no)
    report["rr_floor_changes"] = len(rr_changes)
    if rr_changes:
        report.setdefault("actions", []).extend(rr_changes)
        report.setdefault("warnings", []).extend(rr_changes)
```

(NOTE on the bar window: first-touch scoring walks forward from the veto. Passing the recent `HORIZON`-ish bars is a pragmatic v1 — a vetoed entry resolves once price reaches its TP or stop within the tracked window. If precise from-veto alignment is needed, store the veto timestamp on the entry and slice the frame from there; that refinement can be a follow-up. Keep the helper's `bars_for` seam so this is a one-function change later.)

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_gate_wiring.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add futures_fund/orchestration.py tests/test_gate_wiring.py
git commit -m "feat(gate): reflect-phase shadow scoring + adaptive RR-floor update, surfaced"
```

---

### Task 8: Integration + full suite + lint + memory

**Files:**
- Test: `tests/test_rr_floor.py` (integration), all
- Modify: memory `crypto-team-project` / a new `adaptive-rr-floor` note (optional)

- [ ] **Step 1: Integration test** — seed floor admits nothing sub-2.0; after a quadrant learns to 1.6 a 1.7-RR trade is admitted in that quadrant only.

```python
# tests/test_rr_floor.py
def test_integration_floor_gates_then_admits():
    from futures_fund.rr_floor import effective_rr_floor, adapt_rr_floor, SEED
    st = {q: SEED for q in ("high_vol_trend", "low_vol_trend", "high_vol_range", "low_vol_range")}
    assert effective_rr_floor("low_vol_range", st) == 2.0           # seed: 1.7 trade vetoed
    # learn down over repeated winning samples (8/0 each cycle), clamped at 1.6
    for c in range(1, 12):
        st, _ = adapt_rr_floor(st, {"low_vol_range": (8, 0)}, c)
    assert effective_rr_floor("low_vol_range", st) == 1.6           # admits a 1.7 trade now
    assert effective_rr_floor("high_vol_trend", st) == 2.0          # other quadrants unchanged
```

- [ ] **Step 2: Run full suite + lint**

Run: `uv run pytest -q` → expect all green.
Run: `uv run ruff check futures_fund/ tests/` → confirm ZERO net-new vs baseline (stash-compare per the repo convention).

- [ ] **Step 3: Adversarial review (per repo practice)** — dispatch a review over: protected-module edits are minimal + the `HARD_MIN_RR` wrap genuinely cannot be breached by a corrupt `rr_floor.json`; the seed reproduces today's behaviour exactly; long/short symmetry; arm-time CP9 floor == fire-time gate floor; no other gate limit weakened.

- [ ] **Step 4: Commit + push**

```bash
git add -A && git commit -m "test(rr_floor): integration — seed gates, learned floor admits per-quadrant"
git push origin main
```

- [ ] **Step 5: Memory** — add an `adaptive-rr-floor` auto-memory note (band [1.6,2.5], gate-owned HARD_MIN_RR, seed 2.0, shadow-first-touch signal, slow-loosen/fast-tighten) and link `[[crypto-only-mandate]]`-style; update `MEMORY.md`.

---

## Self-Review

**Spec coverage:** §1 state→T1; §2 effective→T1; §3 gate wrap→T4; §4 cycle threading→T5; §5 CP9→T6; §6a scoring+cache→T3; §6b adapt+trailing→T2; reflect wiring→T7; error-handling/fail-safe→T1/T4; testing→every task + T8. Pin-to-bound vigilance flag: NOT yet a task — **fold a one-liner into T7** (`if a quadrant sits at a bound and changed toward it ≥5 consecutive updated_cycles, append a "pinned" warning"`); add the counter to `rr_floor.json` or compute from a small history. (Acceptable to defer as a follow-up if it complicates T7 — note it in the handoff.)

**Placeholder scan:** the two fixture seams flagged inline (`[...8 levels...]`, `_range_low_vol()`/`_downtrend_then_drop()`) are deliberate fixture-construction notes, not code placeholders — each says exactly how to build the frame (probe the quadrant / pick levels off the bars). The `bars_for` seam is real and named.

**Type consistency:** `effective_rr_floor(quadrant, state)`, `adapt_rr_floor(state, won_lost_by_quadrant, cycle_no) -> (state, changes)`, `score_shadow_first_touch(entry, bars) -> str`, `tally_resolutions(scored, trail_w) -> {q:(won,lost)}`, `GateInputs.rr_floor: float | None`, `HARD_MIN_RR=1.6` — used consistently T1–T8.

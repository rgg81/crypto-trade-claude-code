# Futures-Fund Phase C — Evaluation, Scorecard & Graduation Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Measure whether the edge is *real* and make that measurement visible to the whole team: an equity-history log, performance metrics (Sharpe/Sortino/maxDD/Calmar/hit-rate/profit-factor + per-agent attribution), a DSR-based **graduation gate** (paper→live), **shadow-watch** of vetoed trades, a per-cycle **desk scorecard** injected into EVERY agent prompt, and DSR-gated lesson promotion.

**Architecture:** Pure metric functions over the journal's closed decisions + an equity-history series; the vendored `overfit_detector.deflated_sharpe_ratio` provides DSR. A `scorecard` digest is built each cycle and surfaced to all subagents (the user's explicit requirement) so the team reasons *with* its track record. The graduation verdict + verdict-horizon gate the paper→live transition; the DSR gate also guards lesson promotion (the statistical layer over B3's count-based mechanics). No live LLM/network in tests.

**Tech Stack:** Python 3.11 / uv, pydantic v2, numpy, pytest, ruff. Reuses vendored `futures_fund.vendor.overfit_detector`.

**Reference:** spec §6 (promotion gating), §9 (graduation gate, metrics), §0 (the 5%/mo mandate). Builds on A3a `journal`/`state`, B1 `lessons`, B2 `orchestration`/`cycle_io`/`SKILL.md`, B3 `lessons` promotion.

**Conventions:** 4h cycles → `PERIODS_PER_YEAR = 2190` (6×365). Per-cycle returns come from the equity series; per-trade stats from the journal's closed decisions (`realized_pnl != None`). The monthly target is `0.05`.

---

## File Structure

```
futures_fund/
  equity_log.py    # record_equity / equity_series / returns_series  (+ wired into execute_proposals)
  metrics.py       # sharpe, sortino, max_drawdown, calmar, hit_rate, profit_factor, attribution
  graduation.py    # deflated_sharpe_pvalue (vendored), graduation_verdict, verdict-horizon
  shadow.py        # record_shadow + shadow_outcome (value-of-veto)
  scorecard.py     # build_scorecard: the digest injected into EVERY agent prompt
  cycle.py         # (extend) execute_proposals records equity + vetoed proposals to shadow
  orchestration.py # (extend) preflight context carries the scorecard
  lessons.py       # (no change here; promotion gating added via graduation in T6)
scripts/
  scorecard_cli.py · graduation_cli.py
SKILL.md           # (extend) inject scorecard into every subagent prompt
tests/
  test_equity_log.py · test_metrics.py · test_graduation.py · test_shadow.py
  test_scorecard.py · test_promotion_gate.py
```

---

## Task 1: Equity-history log

**Files:** create `futures_fund/equity_log.py`, `tests/test_equity_log.py`.

- [ ] **Step 1: Write the failing test** — `tests/test_equity_log.py`:

```python
from datetime import datetime, timezone

import pytest

from futures_fund.equity_log import equity_series, record_equity, returns_series

UTC = timezone.utc


def test_record_and_series_roundtrip(tmp_path):
    record_equity(tmp_path, datetime(2026, 5, 1, tzinfo=UTC), 10_000.0, cycle=1)
    record_equity(tmp_path, datetime(2026, 5, 1, 4, tzinfo=UTC), 10_100.0, cycle=2)
    series = equity_series(tmp_path)
    assert [e for _, e in series] == [10_000.0, 10_100.0]


def test_returns_series_is_pct_change(tmp_path):
    record_equity(tmp_path, datetime(2026, 5, 1, tzinfo=UTC), 100.0, cycle=1)
    record_equity(tmp_path, datetime(2026, 5, 1, 4, tzinfo=UTC), 110.0, cycle=2)
    record_equity(tmp_path, datetime(2026, 5, 1, 8, tzinfo=UTC), 99.0, cycle=3)
    rets = returns_series(tmp_path)
    assert rets[0] == pytest.approx(0.10)
    assert rets[1] == pytest.approx(-0.10)


def test_empty_series(tmp_path):
    assert equity_series(tmp_path) == []
    assert returns_series(tmp_path) == []
```

- [ ] **Step 2: Run** `uv run pytest tests/test_equity_log.py -v` — expect FAIL.

- [ ] **Step 3: Implement** `futures_fund/equity_log.py`:

```python
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path


def _path(state_dir) -> Path:
    return Path(state_dir) / "equity-history.jsonl"


def record_equity(state_dir, ts: datetime, equity: float, cycle: int) -> None:
    """Append the desk's total equity at the end of a cycle (the return series' source)."""
    p = _path(state_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as f:
        f.write(json.dumps({"ts": ts.isoformat(), "equity": float(equity), "cycle": cycle}) + "\n")


def equity_series(state_dir) -> list[tuple[str, float]]:
    p = _path(state_dir)
    if not p.exists():
        return []
    out = []
    for line in p.read_text().splitlines():
        if line.strip():
            r = json.loads(line)
            out.append((r["ts"], float(r["equity"])))
    return out


def returns_series(state_dir) -> list[float]:
    eq = [e for _, e in equity_series(state_dir)]
    return [(eq[i] / eq[i - 1] - 1.0) for i in range(1, len(eq)) if eq[i - 1] > 0]
```

- [ ] **Step 4: Wire into the cycle.** In `futures_fund/cycle.py` `execute_proposals`, add at the very end (just before `return report`), after `save_positions(...)`:

```python
    from futures_fund.equity_log import record_equity
    record_equity(state_dir, now, final_health.equity, cycle_no)
```

- [ ] **Step 5: Run** `uv run pytest tests/test_equity_log.py tests/test_cycle.py tests/test_execute_proposals.py -v` — expect PASS (equity_log 3 + the existing cycle tests still green; they now also write an equity line under their tmp state — harmless). Then `uv run ruff check futures_fund/equity_log.py futures_fund/cycle.py tests/test_equity_log.py`.

- [ ] **Step 6: Commit**

```bash
git add futures_fund/equity_log.py futures_fund/cycle.py tests/test_equity_log.py
git commit -m "feat: equity-history log + record each cycle's equity (return series source)"
```

---

## Task 2: Performance metrics

**Files:** create `futures_fund/metrics.py`, `tests/test_metrics.py`.

- [ ] **Step 1: Write the failing test** — `tests/test_metrics.py`:

```python
import pytest

from futures_fund.metrics import (
    agent_attribution,
    calmar,
    hit_rate,
    max_drawdown,
    profit_factor,
    sharpe,
    sortino,
)


def test_sharpe_zero_for_constant_returns():
    assert sharpe([0.01, 0.01, 0.01]) == 0.0
    assert sharpe([]) == 0.0


def test_sharpe_positive_for_positive_mean():
    assert sharpe([0.0, 0.02, 0.01, 0.015]) > 0


def test_sortino_only_penalizes_downside():
    # no negative returns -> sortino is large/inf-guarded but > sharpe-ish; just assert positive
    assert sortino([0.01, 0.02, 0.0]) > 0
    assert sortino([0.01, 0.01]) >= 0


def test_max_drawdown_peak_to_trough():
    assert max_drawdown([100, 110, 90, 95, 120]) == pytest.approx((110 - 90) / 110)


def test_max_drawdown_monotonic_up_is_zero():
    assert max_drawdown([100, 101, 102]) == 0.0


def test_calmar_is_return_over_drawdown():
    assert calmar(annual_return=0.40, mdd=0.10) == pytest.approx(4.0)
    assert calmar(0.40, 0.0) == 0.0  # guard


def test_hit_rate_and_profit_factor():
    closed = [{"realized_pnl": 5.0}, {"realized_pnl": -3.0}, {"realized_pnl": 2.0}]
    assert hit_rate(closed) == pytest.approx(2 / 3)
    assert profit_factor(closed) == pytest.approx(7.0 / 3.0)


def test_profit_factor_no_losses_returns_inf_guard():
    assert profit_factor([{"realized_pnl": 5.0}]) == float("inf")
    assert hit_rate([]) == 0.0


def test_agent_attribution_sums_pnl_and_hit_rate_per_agent():
    closed = [
        {"realized_pnl": 10.0, "contributing_agents": ["research_manager", "trader"]},
        {"realized_pnl": -4.0, "contributing_agents": ["research_manager", "trader"]},
        {"realized_pnl": 6.0, "contributing_agents": ["baseline"]},
    ]
    attr = agent_attribution(closed)
    assert attr["research_manager"]["pnl"] == pytest.approx(6.0)
    assert attr["research_manager"]["count"] == 2
    assert attr["research_manager"]["hit_rate"] == pytest.approx(0.5)
    assert attr["baseline"]["pnl"] == pytest.approx(6.0)
```

- [ ] **Step 2: Run** `uv run pytest tests/test_metrics.py -v` — expect FAIL.

- [ ] **Step 3: Implement** `futures_fund/metrics.py`:

```python
from __future__ import annotations

import numpy as np

PERIODS_PER_YEAR = 2190.0  # 4h cycles: 6/day * 365


def sharpe(returns: list[float], periods_per_year: float = PERIODS_PER_YEAR) -> float:
    if len(returns) < 2:
        return 0.0
    arr = np.asarray(returns, dtype=float)
    sd = arr.std(ddof=1)
    if sd == 0:
        return 0.0
    return float(arr.mean() / sd * np.sqrt(periods_per_year))


def sortino(returns: list[float], periods_per_year: float = PERIODS_PER_YEAR) -> float:
    if len(returns) < 2:
        return 0.0
    arr = np.asarray(returns, dtype=float)
    downside = arr[arr < 0]
    dd = downside.std(ddof=1) if len(downside) >= 2 else 0.0
    if dd == 0:
        # no measurable downside: infinite Sortino if net positive, else 0 (like profit_factor)
        return float("inf") if arr.mean() > 0 else 0.0
    return float(arr.mean() / dd * np.sqrt(periods_per_year))


def max_drawdown(equity: list[float]) -> float:
    """Largest peak-to-trough decline as a positive fraction (0 if monotonic up / too short)."""
    if len(equity) < 2:
        return 0.0
    peak = equity[0]
    mdd = 0.0
    for e in equity:
        peak = max(peak, e)
        if peak > 0:
            mdd = max(mdd, (peak - e) / peak)
    return mdd


def calmar(annual_return: float, mdd: float) -> float:
    return annual_return / mdd if mdd > 0 else 0.0


def hit_rate(closed: list[dict]) -> float:
    if not closed:
        return 0.0
    wins = sum(1 for d in closed if d["realized_pnl"] > 0)
    return wins / len(closed)


def profit_factor(closed: list[dict]) -> float:
    gains = sum(d["realized_pnl"] for d in closed if d["realized_pnl"] > 0)
    losses = -sum(d["realized_pnl"] for d in closed if d["realized_pnl"] < 0)
    if losses == 0:
        return float("inf") if gains > 0 else 0.0
    return gains / losses


def agent_attribution(closed: list[dict]) -> dict[str, dict]:
    """Per-agent realized PnL, trade count, and hit-rate. A trade credits every agent in its
    `contributing_agents` list (falls back to 'unknown')."""
    out: dict[str, dict] = {}
    for d in closed:
        agents = d.get("contributing_agents") or ["unknown"]
        for a in agents:
            rec = out.setdefault(a, {"pnl": 0.0, "count": 0, "wins": 0})
            rec["pnl"] += d["realized_pnl"]
            rec["count"] += 1
            rec["wins"] += 1 if d["realized_pnl"] > 0 else 0
    for rec in out.values():
        rec["hit_rate"] = rec["wins"] / rec["count"] if rec["count"] else 0.0
    return out
```

- [ ] **Step 4: Run** `uv run pytest tests/test_metrics.py -v` — expect PASS (9 passed). Then `uv run ruff check futures_fund/metrics.py tests/test_metrics.py`.

- [ ] **Step 5: Commit**

```bash
git add futures_fund/metrics.py tests/test_metrics.py
git commit -m "feat: performance metrics (Sharpe/Sortino/maxDD/Calmar/hit-rate/profit-factor/attribution)"
```

---

## Task 3: DSR + graduation gate

**Files:** create `futures_fund/graduation.py`, `tests/test_graduation.py`.

- [ ] **Step 1: Write the failing test** — `tests/test_graduation.py`:

```python
from futures_fund.graduation import deflated_sharpe_pvalue, graduation_verdict


def test_deflated_sharpe_pvalue_in_unit_interval():
    rets = [0.01, -0.005, 0.02, 0.0, 0.015, -0.01, 0.012] * 5
    p = deflated_sharpe_pvalue(rets, num_trials=10)
    assert 0.0 <= p <= 1.0


def test_deflated_sharpe_pvalue_empty_is_zero():
    assert deflated_sharpe_pvalue([], num_trials=5) == 0.0


def test_verdict_graduated_when_all_criteria_met():
    v = graduation_verdict(n_cycles=30, sharpe=2.0, dsr_pvalue=0.97, beats_baseline=True,
                           max_dd=0.08, min_cycles=20, horizon_cycles=120)
    assert v["status"] == "graduated"
    assert v["reasons"] == []


def test_verdict_not_yet_lists_failing_criteria():
    v = graduation_verdict(n_cycles=10, sharpe=-0.5, dsr_pvalue=0.5, beats_baseline=False,
                           max_dd=0.2, min_cycles=20, horizon_cycles=120)
    assert v["status"] == "not_yet"
    assert any("cycles" in r for r in v["reasons"])
    assert any("DSR" in r for r in v["reasons"])
    assert any("baseline" in r for r in v["reasons"])


def test_verdict_failed_past_horizon_without_edge():
    v = graduation_verdict(n_cycles=130, sharpe=0.1, dsr_pvalue=0.4, beats_baseline=False,
                           max_dd=0.2, min_cycles=20, horizon_cycles=120)
    assert v["status"] == "failed"
```

- [ ] **Step 2: Run** `uv run pytest tests/test_graduation.py -v` — expect FAIL.

- [ ] **Step 3: Implement** `futures_fund/graduation.py`:

```python
from __future__ import annotations

from futures_fund.metrics import PERIODS_PER_YEAR, sharpe
from futures_fund.vendor.overfit_detector import deflated_sharpe_ratio

DSR_THRESHOLD = 0.95


def deflated_sharpe_pvalue(returns: list[float], num_trials: int,
                           periods_per_year: float = PERIODS_PER_YEAR) -> float:
    """Probability the desk's Sharpe is genuinely > 0 after deflating for multiple testing
    (vendored Lopez de Prado DSR). 0.0 if < 10 observations (the vendored DSR requires
    backtest_length >= 10)."""
    if len(returns) < 10:
        return 0.0
    # Pass the RAW per-period Sharpe (mean/std). The DSR standard error is computed from
    # backtest_length, so feeding an annualized SR would be a scale mismatch.
    observed = sharpe(returns, periods_per_year=1.0)
    result = deflated_sharpe_ratio(observed_sr=observed, num_trials=max(1, num_trials),
                                   backtest_length=len(returns))
    return float(result.dsr_pvalue)


def graduation_verdict(n_cycles: int, sharpe: float, dsr_pvalue: float, beats_baseline: bool,
                       max_dd: float, *, min_cycles: int = 20, horizon_cycles: int = 120,
                       dsr_threshold: float = DSR_THRESHOLD) -> dict:
    """Decide paper->live readiness. graduated only if ALL criteria pass; failed if past the
    verdict horizon without an edge; otherwise not_yet with the failing criteria listed."""
    reasons: list[str] = []
    if n_cycles < min_cycles:
        reasons.append(f"need >= {min_cycles} audited cycles (have {n_cycles})")
    if sharpe <= 0:
        reasons.append(f"OOS Sharpe must be > 0 (is {sharpe:.2f})")
    if dsr_pvalue < dsr_threshold:
        reasons.append(f"DSR {dsr_pvalue:.2f} < {dsr_threshold} (edge not statistically proven)")
    if not beats_baseline:
        reasons.append("must beat buy-&-hold baseline net of costs")
    if not reasons:
        return {"status": "graduated", "reasons": []}
    if n_cycles >= horizon_cycles:
        return {"status": "failed", "reasons": reasons + [
            f"verdict horizon ({horizon_cycles} cycles) reached without an edge — retire/redesign"]}
    return {"status": "not_yet", "reasons": reasons}
```

- [ ] **Step 4: Run** `uv run pytest tests/test_graduation.py -v` — expect PASS (5 passed). Then `uv run ruff check futures_fund/graduation.py tests/test_graduation.py`.

- [ ] **Step 5: Commit**

```bash
git add futures_fund/graduation.py tests/test_graduation.py
git commit -m "feat: DSR p-value + graduation verdict (cycles/Sharpe/DSR/baseline + verdict horizon)"
```

---

## Task 4: Desk scorecard (injected into EVERY agent prompt)

**Files:** create `futures_fund/scorecard.py`, `tests/test_scorecard.py`; modify `futures_fund/orchestration.py` (preflight carries the scorecard) and `SKILL.md` (inject it into every subagent prompt).

- [ ] **Step 1: Write the failing test** — `tests/test_scorecard.py`:

```python
from datetime import datetime, timezone

from futures_fund.equity_log import record_equity
from futures_fund.journal import append_decision, patch_outcome
from futures_fund.memory_layout import ensure_memory_layout
from futures_fund.scorecard import build_scorecard

UTC = timezone.utc


def _seed(state_dir, memory_dir):
    ensure_memory_layout(memory_dir)
    for i, eq in enumerate([10_000, 10_200, 10_100, 10_500], start=1):
        record_equity(state_dir, datetime(2026, 5, 1, 4 * i, tzinfo=UTC), float(eq), cycle=i)
    for pnl, agents in [(200.0, ["team"]), (-100.0, ["team"]), (400.0, ["team"])]:
        did = append_decision(memory_dir, {"ts": datetime(2026, 5, 1, tzinfo=UTC), "cycle": 1,
                                           "symbol": "BTCUSDT", "direction": "long",
                                           "entry": 100.0, "stop": 95.0,
                                           "contributing_agents": agents})
        patch_outcome(memory_dir, did, {"realized_pnl": pnl, "prediction_correct": pnl > 0})


def test_scorecard_has_headline_stats_and_target(tmp_path):
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    _seed(state_dir, memory_dir)
    sc = build_scorecard(state_dir, memory_dir, monthly_target=0.05)
    assert sc["equity"] == 10_500.0
    assert sc["monthly_target"] == 0.05
    assert "sharpe" in sc and "max_drawdown" in sc and "hit_rate" in sc
    assert sc["n_closed"] == 3
    assert sc["hit_rate"] > 0.5  # 2 of 3 wins
    assert "team" in sc["agent_hit_rates"]
    assert sc["graduation"]["status"] in {"graduated", "not_yet", "failed"}


def test_scorecard_warns_in_drawdown(tmp_path):
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    ensure_memory_layout(memory_dir)
    for i, eq in enumerate([10_000, 9_000], start=1):  # -10% drawdown
        record_equity(state_dir, datetime(2026, 5, 1, 4 * i, tzinfo=UTC), float(eq), cycle=i)
    sc = build_scorecard(state_dir, memory_dir, monthly_target=0.05)
    assert any("drawdown" in w.lower() for w in sc["warnings"])


def test_scorecard_empty_history_is_safe(tmp_path):
    sc = build_scorecard(tmp_path / "s", tmp_path / "m", monthly_target=0.05)
    assert sc["equity"] is None and sc["n_closed"] == 0
```

- [ ] **Step 2: Run** `uv run pytest tests/test_scorecard.py -v` — expect FAIL.

- [ ] **Step 3: Implement** `futures_fund/scorecard.py`:

```python
from __future__ import annotations

from futures_fund.equity_log import equity_series, returns_series
from futures_fund.graduation import deflated_sharpe_pvalue, graduation_verdict
from futures_fund.journal import read_all_decisions
from futures_fund.metrics import (
    agent_attribution,
    hit_rate,
    max_drawdown,
    profit_factor,
    sharpe,
    sortino,
)


def build_scorecard(state_dir, memory_dir, monthly_target: float = 0.05,
                    min_cycles: int = 20, horizon_cycles: int = 120) -> dict:
    """The desk's statistical self-portrait — injected into EVERY agent prompt so the team
    reasons WITH its measured track record (equity, return vs target, drawdown, risk-adjusted
    returns, per-agent hit-rates, graduation status, and warnings)."""
    eq = [e for _, e in equity_series(state_dir)]
    rets = returns_series(state_dir)
    closed = [d for d in read_all_decisions(memory_dir) if d.get("realized_pnl") is not None]
    n_cycles = len(eq)

    if not eq:
        return {"equity": None, "monthly_target": monthly_target, "n_cycles": 0, "n_closed": 0,
                "sharpe": 0.0, "sortino": 0.0, "max_drawdown": 0.0, "hit_rate": 0.0,
                "profit_factor": 0.0, "period_return": 0.0, "agent_hit_rates": {},
                "graduation": graduation_verdict(0, 0.0, 0.0, False, 0.0,
                                                 min_cycles=min_cycles, horizon_cycles=horizon_cycles),
                "warnings": ["no equity history yet — desk is cold-starting"]}

    period_return = eq[-1] / eq[0] - 1.0
    mdd = max_drawdown(eq)
    shp = sharpe(rets)
    dsr = deflated_sharpe_pvalue(rets, num_trials=10)  # conservative fixed trial count (not cycle count)
    beats_baseline = period_return > 0  # vs flat cash; a price baseline can refine this later
    grad = graduation_verdict(n_cycles, shp, dsr, beats_baseline, mdd,
                              min_cycles=min_cycles, horizon_cycles=horizon_cycles)
    attr = agent_attribution(closed)
    hr = hit_rate(closed)

    warnings: list[str] = []
    if mdd >= 0.05:
        warnings.append(f"in drawdown: {mdd:.0%} from peak — bias risk-off")
    if n_cycles >= 11 and dsr < 0.95:  # DSR only computable at >=10 returns (>=11 equity points)
        warnings.append("edge not statistically proven (DSR < 0.95) — size conservatively")
    if n_cycles >= 6 and period_return < monthly_target * (n_cycles / 180.0):
        warnings.append(f"running below the {monthly_target:.0%}/mo target — do not force trades")

    return {
        "equity": eq[-1], "monthly_target": monthly_target, "n_cycles": n_cycles,
        "n_closed": len(closed), "period_return": period_return,
        "sharpe": shp, "sortino": sortino(rets), "max_drawdown": mdd,
        "hit_rate": hr, "profit_factor": profit_factor(closed),
        "dsr_pvalue": dsr,
        "agent_hit_rates": {a: round(r["hit_rate"], 3) for a, r in attr.items()},
        "graduation": grad, "warnings": warnings,
    }
```

- [ ] **Step 4: Wire the scorecard into preflight context.** In `futures_fund/orchestration.py` `preflight_step`, after computing `health` (and before/with building the return dict), add the scorecard and include it in BOTH the normal and the halted return dicts:

```python
    from futures_fund.scorecard import build_scorecard
    scorecard = build_scorecard(state_dir, memory_dir, monthly_target=0.05)
```
Add `"scorecard": scorecard,` to the returned context dict (the non-halted return). For the halted early-return, add `"scorecard": build_scorecard(state_dir, memory_dir)` too.

- [ ] **Step 5: Wire into `SKILL.md`.** In the `## Subagent dispatch rules` section, change the first bullet (mission injection) to:

```markdown
- Inject `MISSION.md` verbatim AND the cycle scorecard (`state/cycle/N/context.json` → `scorecard`) at the top of EVERY subagent prompt. The scorecard is the desk's statistical self-portrait (equity, return vs the 5%/mo target, drawdown, Sharpe/Sortino, hit-rate, profit factor, per-agent hit-rates, graduation status, and warnings). Every agent must reason WITH these numbers — e.g. bias risk-off in drawdown, size conservatively when the edge is statistically unproven, and never force trades to chase the target.
```

- [ ] **Step 6: Run** `uv run pytest tests/test_scorecard.py tests/test_orchestration.py -v` — expect PASS (scorecard 3 + orchestration still green). Then `uv run ruff check futures_fund/scorecard.py futures_fund/orchestration.py tests/test_scorecard.py`.

- [ ] **Step 7: Commit**

```bash
git add futures_fund/scorecard.py futures_fund/orchestration.py SKILL.md tests/test_scorecard.py
git commit -m "feat: desk scorecard (stats digest) injected into every agent prompt"
```

---

## Task 5: Shadow-watch (value of the risk veto)

**Files:** create `futures_fund/shadow.py`, `tests/test_shadow.py`; modify `futures_fund/cycle.py` (`execute_proposals` records vetoed proposals).

- [ ] **Step 1: Write the failing test** — `tests/test_shadow.py`:

```python
from datetime import datetime, timezone

from futures_fund.shadow import record_shadow, shadow_ledger, shadow_outcome

UTC = timezone.utc


def test_record_and_read_shadow(tmp_path):
    record_shadow(tmp_path, datetime(2026, 5, 1, tzinfo=UTC), cycle=1, entries=[
        {"symbol": "BTCUSDT", "direction": "long", "entry": 100.0, "stop": 95.0,
         "take_profits": [115.0], "reason": "RR 1.2 < min 2"}])
    led = shadow_ledger(tmp_path)
    assert len(led) == 1 and led[0]["symbol"] == "BTCUSDT" and led[0]["cycle"] == 1


def test_shadow_outcome_long_would_have_stopped_out():
    entry = {"symbol": "BTCUSDT", "direction": "long", "entry": 100.0, "stop": 95.0,
             "take_profits": [115.0]}
    # bar low pierces the stop -> the vetoed long would have lost; veto SAVED us
    out = shadow_outcome(entry, bar_high=101.0, bar_low=94.0)
    assert out["hit"] == "stop" and out["r_multiple"] < 0 and out["veto_saved"] is True


def test_shadow_outcome_long_would_have_won():
    entry = {"symbol": "BTCUSDT", "direction": "long", "entry": 100.0, "stop": 95.0,
             "take_profits": [115.0]}
    out = shadow_outcome(entry, bar_high=116.0, bar_low=99.0)
    assert out["hit"] == "take_profit" and out["r_multiple"] > 0 and out["veto_saved"] is False


def test_shadow_outcome_no_trigger():
    entry = {"symbol": "BTCUSDT", "direction": "long", "entry": 100.0, "stop": 95.0,
             "take_profits": [115.0]}
    assert shadow_outcome(entry, bar_high=108.0, bar_low=98.0)["hit"] is None
```

- [ ] **Step 2: Run** `uv run pytest tests/test_shadow.py -v` — expect FAIL.

- [ ] **Step 3: Implement** `futures_fund/shadow.py`:

```python
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path


def _path(state_dir) -> Path:
    return Path(state_dir) / "shadow-ledger.jsonl"


def record_shadow(state_dir, ts: datetime, cycle: int, entries: list[dict]) -> int:
    """Record proposals the risk gate VETOED (at zero capital) so we can later measure whether
    the veto saved or cost us — the value of the risk filter (spec §9)."""
    p = _path(state_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as f:
        for e in entries:
            f.write(json.dumps({**e, "ts": ts.isoformat(), "cycle": cycle}) + "\n")
    return len(entries)


def shadow_ledger(state_dir) -> list[dict]:
    p = _path(state_dir)
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


def shadow_outcome(entry: dict, bar_high: float, bar_low: float) -> dict:
    """Hypothetical outcome of a vetoed trade over one bar (R-multiple if stop/tp touched).
    `veto_saved` is True when the would-be trade would have lost (so vetoing it was correct)."""
    e, stop = entry["entry"], entry["stop"]
    tp = entry["take_profits"][0] if entry.get("take_profits") else None
    risk = abs(e - stop)
    hit, level = None, None
    if entry["direction"] == "long":
        if bar_low <= stop:
            hit, level = "stop", stop
        elif tp is not None and bar_high >= tp:
            hit, level = "take_profit", tp
    else:
        if bar_high >= stop:
            hit, level = "stop", stop
        elif tp is not None and bar_low <= tp:
            hit, level = "take_profit", tp
    if hit is None:
        return {"hit": None, "r_multiple": 0.0, "veto_saved": False}
    gain = (level - e) if entry["direction"] == "long" else (e - level)
    r = gain / risk if risk > 0 else 0.0
    return {"hit": hit, "r_multiple": r, "veto_saved": r < 0}
```

- [ ] **Step 4: Record vetoes in the cycle.** In `futures_fund/cycle.py` `execute_proposals`, the proposals that the gate does NOT approve/resize are currently dropped. Capture them: where the loop builds `approved`, also collect vetoed entries, and after consolidation record them. Concretely, in the `for prop in proposals:` gate loop, in the `else` of the approve/resize check, append to a `vetoed` list; then after persisting, record them:

```python
    # (inside the gate loop, when decision.verdict not in approve/resize:)
            vetoed.append({"symbol": prop.symbol, "direction": prop.direction,
                           "entry": prop.entry, "stop": prop.stop,
                           "take_profits": prop.take_profits, "reason": decision.reason})
    # (after save_positions / record_equity, before return:)
    from futures_fund.shadow import record_shadow
    if vetoed:
        record_shadow(state_dir, now, cycle_no, vetoed)
        report["vetoed"] = len(vetoed)
```
Initialize `vetoed: list = []` before the gate loop and `report.setdefault("vetoed", 0)`.

- [ ] **Step 5: Run** `uv run pytest tests/test_shadow.py tests/test_cycle.py tests/test_execute_proposals.py -v` — expect PASS (shadow 4 + cycle/execute tests still green). Then `uv run ruff check futures_fund/shadow.py futures_fund/cycle.py tests/test_shadow.py`.

- [ ] **Step 6: Commit**

```bash
git add futures_fund/shadow.py futures_fund/cycle.py tests/test_shadow.py
git commit -m "feat: shadow-watch vetoed trades (measure the value of the risk filter)"
```

---

## Task 6: DSR-gated lesson promotion + CLIs + full suite

**Files:** modify `futures_fund/lessons.py` (add a gated promote helper); create `scripts/scorecard_cli.py`, `scripts/graduation_cli.py`, `tests/test_promotion_gate.py`.

- [ ] **Step 1: Write the failing test** — `tests/test_promotion_gate.py`:

```python
from datetime import datetime, timezone

from futures_fund.lessons import append_lesson, read_lessons, statistically_promote

UTC = timezone.utc


def _add(tmp_path):
    return append_lesson(tmp_path, {"text": "x", "regime": "high_vol_trend", "tags": ["t"],
                                    "confirmations": 4}, ts=datetime(2026, 5, 1, tzinfo=UTC))


def test_promote_blocked_when_edge_not_significant(tmp_path):
    lid = _add(tmp_path)
    # 5th confirmation would hit threshold, but DSR below gate -> stays candidate
    statistically_promote(tmp_path, lid, dsr_pvalue=0.5, promote_threshold=5)
    assert next(z for z in read_lessons(tmp_path) if z.id == lid).state == "candidate"


def test_promote_allowed_when_edge_significant(tmp_path):
    lid = _add(tmp_path)
    statistically_promote(tmp_path, lid, dsr_pvalue=0.97, promote_threshold=5)
    lz = next(z for z in read_lessons(tmp_path) if z.id == lid)
    assert lz.state == "validated" and lz.confirmations == 5
```

- [ ] **Step 2: Run** `uv run pytest tests/test_promotion_gate.py -v` — expect FAIL.

- [ ] **Step 3: Implement** — append to `futures_fund/lessons.py`:

```python
def statistically_promote(memory_dir, lesson_id: str, *, dsr_pvalue: float,
                          promote_threshold: int = 5, dsr_threshold: float = 0.95) -> bool:
    """Confirm a lesson, but only allow CANDIDATE->VALIDATED promotion when the desk's edge is
    statistically proven (DSR p-value >= threshold). Below the gate the confirmation still
    counts, but the lesson stays CANDIDATE — the statistical layer over B3's count-based rule
    (spec §6). Returns True if the lesson was found."""
    lessons = read_lessons(memory_dir)
    hit = False
    for i, lz in enumerate(lessons):
        if lz.id == lesson_id:
            c = lz.confirmations + 1
            promote = (lz.state == "candidate" and c >= promote_threshold
                       and dsr_pvalue >= dsr_threshold)
            lessons[i] = lz.model_copy(update={"confirmations": c,
                                               "state": "validated" if promote else lz.state})
            hit = True
    if hit:
        _write_all(memory_dir, lessons)
    return hit
```

- [ ] **Step 4: Create the CLIs.** `scripts/scorecard_cli.py`:

```python
"""Print the desk scorecard (the stats digest injected into every agent prompt).

    uv run python scripts/scorecard_cli.py
"""
from __future__ import annotations

import json

from futures_fund.scorecard import build_scorecard


def main() -> None:
    print(json.dumps(build_scorecard("state", "memory", monthly_target=0.05), indent=2, default=str))


if __name__ == "__main__":
    main()
```

`scripts/graduation_cli.py`:

```python
"""Print the graduation verdict (paper -> live readiness).

    uv run python scripts/graduation_cli.py
"""
from __future__ import annotations

import json

from futures_fund.scorecard import build_scorecard


def main() -> None:
    sc = build_scorecard("state", "memory", monthly_target=0.05)
    print(json.dumps(sc["graduation"], indent=2, default=str))


if __name__ == "__main__":
    main()
```

- [ ] **Step 5: Run** `uv run pytest tests/test_promotion_gate.py -v` — expect PASS (2 passed). Then `uv run ruff check futures_fund/lessons.py scripts/scorecard_cli.py scripts/graduation_cli.py tests/test_promotion_gate.py`.

- [ ] **Step 6: Run the FULL suite + lint** `uv run pytest` then `uv run ruff check .`. Report the EXACT total (expected 199 + equity_log 3 + metrics 9 + graduation 5 + scorecard 3 + shadow 4 + promotion_gate 2 = **225**).

- [ ] **Step 7: Commit**

```bash
git add futures_fund/lessons.py scripts/scorecard_cli.py scripts/graduation_cli.py tests/test_promotion_gate.py
git commit -m "feat: DSR-gated lesson promotion + scorecard/graduation CLIs"
```

---

## Self-Review (completed during planning)

**Spec coverage (§9 eval/graduation, §6 promotion, §0 the 5%/mo mandate, + the user's 'stats to all agents' requirement):** equity history + return series ✓ (T1); Sharpe/Sortino/maxDD/Calmar/hit-rate/profit-factor + per-agent attribution ✓ (T2); DSR + graduation verdict + verdict horizon ✓ (T3); **the scorecard injected into EVERY agent prompt** ✓ (T4 — the headline requirement); shadow-watch of vetoed trades ✓ (T5); DSR-gated lesson promotion ✓ (T6). Deferred to D (correct): the price-based buy-&-hold baseline refinement (T4 uses return>0 vs cash as a placeholder); full PBO via CPCV (the vendored harness is available for offline backtest validation; the live paper gate uses DSR).

**Placeholder scan:** none — runnable code/tests + exact edits.

**Type/interface consistency:** `deflated_sharpe_pvalue` uses the vendored `deflated_sharpe_ratio(observed_sr, num_trials, backtest_length)` returning `DSRResult.dsr_pvalue` (the field confirmed in A2 vendoring). `metrics`/`graduation`/`scorecard` compose on plain lists/dicts from `equity_log` + the journal's closed decisions. `build_scorecard` is added to the preflight context and injected per SKILL.md. `execute_proposals` gains equity recording (T1) + veto recording (T5) — both additive, guarded by the unchanged cycle/execute_proposals tests. `statistically_promote` reuses B3's `_write_all`.

**Note (the user's requirement):** the scorecard is the deliberate mechanism for making statistical information available to all agents — built in preflight, persisted to `state/cycle/N/context.json`, and SKILL.md mandates injecting it into every subagent prompt alongside MISSION.

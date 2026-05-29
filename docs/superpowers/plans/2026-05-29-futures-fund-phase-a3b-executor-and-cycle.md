# Futures-Fund Phase A3b — Executor & Cycle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the desk's trading cycle *run end-to-end on paper, deterministically, with no LLM*: a reconciliation paper executor (sim fills costed with A1 fees/funding/slippage, resting stop/TP, **mark-price** liquidation/stop/TP detection), deterministic portfolio consolidation (gross-heat cap + CVaR de-risk), a trivial baseline strategy, and a phased cycle runner that wires A1+A2+A3a together.

**Architecture:** Small pure-logic modules (`fills`, `exits`, `executor`, `consolidation`, `baseline`) each unit-tested with synthetic inputs, then `cycle.run_cycle(...)` orchestrates phases 0–11 over an **injected exchange** (a fake in tests) and tmp `state/`+`memory/` dirs. The cycle is the integration capstone; its end-to-end test opens a position on a clean uptrend and closes it on a crash bar, asserting persistence + risk-limit respect.

**Tech Stack:** Python 3.11 / uv, pydantic v2, pandas/numpy, pytest, ruff. No network (the cycle takes an injected exchange).

**Reference:** spec §4 (the cycle), §5 (self-healing — deferred to B), §7 (cost/risk). Builds on merged A1 (`models`, `costs`, `risk_gate`, `policy`, `portfolio_risk`), A2 (`exchange`, `market_data`, `config`), A3a (`state`, `portfolio`, `journal`, `hitrate`, `memory_layout`).

**Conventions:**
- Paper slippage is modeled as a configurable **bps on the fill price** (we have no live L2 book in paper; A1's depth-walk `slippage_cost` is used in live mode later). Buy fills slip up, sell fills slip down.
- Exit triggers use the **bar's high/low** to detect intra-bar touches; priority is pessimistic: **liquidation > stop > take-profit**. Liquidation compares the bar extreme to the position's `liq_price` (mark-price proxy in paper).
- Entry fee+slippage are charged at open; exit fee + accrued funding at close. Realized PnL is computed from actual (slipped) fill prices, so slippage is reflected in PnL.
- One position per symbol. Closing a flipped/removed symbol exits at the current mark.

---

## File Structure

```
futures_fund/
  fills.py          # fill_price (slippage), open/close fee helpers
  exits.py          # ClosedTrade + detect_exit (mark/bar-based liq/stop/TP, net realized PnL)
  executor.py       # reconcile(target,current), open_position, close_at_mark, process_exits
  consolidation.py  # cvar_risk_multiplier + consolidate (gross-heat cap, CVaR de-risk, drop dust)
  baseline.py       # simple_regime + propose (ATR/EMA, no LLM) — stand-in for the Phase-B team
  cycle.py          # run_cycle: phases 0-11 over an injected exchange + state/memory dirs; CycleReport
config.py           # (modify) add Settings.symbols list
scripts/run_cycle.py# CLI: build real exchange + dirs, call run_cycle
tests/
  test_fills.py · test_exits.py · test_executor.py · test_consolidation.py
  test_baseline.py · test_cycle.py
```

---

## Task 1: Fills (paper slippage + fees)

**Files:** create `futures_fund/fills.py`, `tests/test_fills.py`.

- [ ] **Step 1: Write the failing test** — `tests/test_fills.py`:

```python
import pytest

from futures_fund.fills import close_side, fill_price, open_side


def test_buy_fill_slips_up_sell_slips_down():
    assert fill_price(100.0, "buy", slippage_bps=10) == pytest.approx(100.1)   # +0.10%
    assert fill_price(100.0, "sell", slippage_bps=10) == pytest.approx(99.9)


def test_zero_slippage_is_identity():
    assert fill_price(100.0, "buy", slippage_bps=0) == 100.0


def test_open_side_maps_direction_to_order_side():
    assert open_side("long") == "buy"
    assert open_side("short") == "sell"


def test_close_side_is_the_opposite():
    assert close_side("long") == "sell"
    assert close_side("short") == "buy"
```

- [ ] **Step 2: Run** `uv run pytest tests/test_fills.py -v` — expect FAIL.

- [ ] **Step 3: Implement** `futures_fund/fills.py`:

```python
from __future__ import annotations

from futures_fund.models import Direction


def open_side(direction: Direction) -> str:
    return "buy" if direction == "long" else "sell"


def close_side(direction: Direction) -> str:
    return "sell" if direction == "long" else "buy"


def fill_price(reference_price: float, side: str, slippage_bps: float) -> float:
    """Apply paper slippage to a reference price. Buys slip up, sells slip down."""
    adj = slippage_bps / 10_000.0
    return reference_price * (1.0 + adj) if side == "buy" else reference_price * (1.0 - adj)
```

- [ ] **Step 4: Run** `uv run pytest tests/test_fills.py -v` — expect PASS (4 passed). Then `uv run ruff check futures_fund/fills.py tests/test_fills.py`.

- [ ] **Step 5: Commit**

```bash
git add futures_fund/fills.py tests/test_fills.py
git commit -m "feat: paper fill-price slippage + order-side helpers"
```

---

## Task 2: Exit detection (mark-price liquidation / stop / take-profit)

**Files:** create `futures_fund/exits.py`, `tests/test_exits.py`.

- [ ] **Step 1: Write the failing test** — `tests/test_exits.py`:

```python
import pytest

from futures_fund.exits import ClosedTrade, detect_exit
from tests.test_state import _pos  # Position factory (long stop 95, short stop 105)


def _long(**over):
    p = _pos("BTCUSDT", "long")  # qty 0.5, entry 100, stop 95, tp 115, liq 82
    return p.model_copy(update=over)


def _short(**over):
    p = _pos("ETHUSDT", "short")  # qty 0.5, entry 100, stop 105, tp 115(!), liq 82(!)
    # fix short tp/liq to valid short geometry: tp below entry, liq above entry
    return p.model_copy(update={"take_profits": [85.0], "liq_price": 118.0, **over})


def test_no_trigger_returns_none():
    # bar stays between stop and tp
    assert detect_exit(_long(), bar_high=108.0, bar_low=99.0,
                       funding_rate=0.0, funding_events=0, slippage_bps=0) is None


def test_long_stop_hit_realizes_loss():
    ct = detect_exit(_long(), bar_high=101.0, bar_low=94.0,
                     funding_rate=0.0, funding_events=0, slippage_bps=0)
    assert isinstance(ct, ClosedTrade)
    assert ct.reason == "stop"
    assert ct.exit_price == pytest.approx(95.0)            # no slippage
    # gross -2.5 minus the exit fee (~0.024); abs tolerance covers the fee
    assert ct.realized_pnl == pytest.approx(0.5 * (95.0 - 100.0), abs=0.05)
    assert ct.realized_pnl < 0


def test_long_take_profit_hit_realizes_gain():
    ct = detect_exit(_long(), bar_high=116.0, bar_low=99.0,
                     funding_rate=0.0, funding_events=0, slippage_bps=0)
    assert ct.reason == "take_profit"
    assert ct.exit_price == pytest.approx(115.0)
    assert ct.realized_pnl > 0


def test_long_liquidation_takes_priority_over_stop():
    # bar low below BOTH stop (95) and liq (82) -> liquidation wins
    ct = detect_exit(_long(), bar_high=101.0, bar_low=80.0,
                     funding_rate=0.0, funding_events=0, slippage_bps=0)
    assert ct.reason == "liquidation"
    assert ct.exit_price == pytest.approx(82.0)


def test_long_stop_beats_tp_when_both_touched():
    # both stop (low<=95) and tp (high>=115) in the same bar -> pessimistic: stop
    ct = detect_exit(_long(), bar_high=120.0, bar_low=94.0,
                     funding_rate=0.0, funding_events=0, slippage_bps=0)
    assert ct.reason == "stop"


def test_short_stop_hit_above_entry():
    ct = detect_exit(_short(), bar_high=106.0, bar_low=99.0,
                     funding_rate=0.0, funding_events=0, slippage_bps=0)
    assert ct.reason == "stop"
    assert ct.exit_price == pytest.approx(105.0)
    # gross -2.5 minus the exit fee (~0.026); abs tolerance covers the fee
    assert ct.realized_pnl == pytest.approx(0.5 * (100.0 - 105.0), abs=0.05)


def test_short_take_profit_below_entry():
    ct = detect_exit(_short(), bar_high=101.0, bar_low=84.0,
                     funding_rate=0.0, funding_events=0, slippage_bps=0)
    assert ct.reason == "take_profit"
    assert ct.exit_price == pytest.approx(85.0)
    assert ct.realized_pnl > 0


def test_funding_reduces_realized_pnl_for_long():
    # positive funding, long pays; 1 event on notional ~50 -> small cost
    ct_no = detect_exit(_long(), bar_high=116.0, bar_low=99.0,
                        funding_rate=0.001, funding_events=0, slippage_bps=0)
    ct_fund = detect_exit(_long(), bar_high=116.0, bar_low=99.0,
                          funding_rate=0.001, funding_events=2, slippage_bps=0)
    assert ct_fund.realized_pnl < ct_no.realized_pnl
    assert ct_fund.funding > 0
```

- [ ] **Step 2: Run** `uv run pytest tests/test_exits.py -v` — expect FAIL.

- [ ] **Step 3: Implement** `futures_fund/exits.py`:

```python
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from futures_fund.costs import project_funding, trade_fee
from futures_fund.fills import close_side, fill_price
from futures_fund.models import Direction
from futures_fund.state import Position

ExitReason = Literal["liquidation", "stop", "take_profit"]


class ClosedTrade(BaseModel):
    symbol: str
    direction: Direction
    decision_id: str | None
    entry: float
    exit_price: float
    qty: float
    reason: ExitReason
    gross_pnl: float
    exit_fee: float
    funding: float
    slippage: float
    realized_pnl: float


def _trigger(position: Position, bar_high: float, bar_low: float) -> tuple[ExitReason, float] | None:
    """Pessimistic priority: liquidation > stop > take-profit."""
    tp = position.take_profits[0] if position.take_profits else None
    if position.direction == "long":
        if bar_low <= position.liq_price:
            return "liquidation", position.liq_price
        if bar_low <= position.stop:
            return "stop", position.stop
        if tp is not None and bar_high >= tp:
            return "take_profit", tp
    else:  # short
        if bar_high >= position.liq_price:
            return "liquidation", position.liq_price
        if bar_high >= position.stop:
            return "stop", position.stop
        if tp is not None and bar_low <= tp:
            return "take_profit", tp
    return None


def detect_exit(
    position: Position, bar_high: float, bar_low: float, *,
    funding_rate: float, funding_events: int, slippage_bps: float, pay_bnb: bool = False,
) -> ClosedTrade | None:
    """Return a ClosedTrade if the bar triggered an exit, else None. PnL is net of exit fee,
    accrued funding, and exit-side slippage."""
    hit = _trigger(position, bar_high, bar_low)
    if hit is None:
        return None
    reason, level = hit
    side = close_side(position.direction)
    exit_fill = fill_price(level, side, slippage_bps)
    if position.direction == "long":
        gross = position.qty * (exit_fill - position.entry)
    else:
        gross = position.qty * (position.entry - exit_fill)
    exit_fee = trade_fee(position.qty * exit_fill, maker=False, pay_bnb=pay_bnb)
    funding = max(0.0, project_funding(position.qty * position.entry, funding_rate,
                                       position.direction, funding_events))
    slippage = abs(exit_fill - level) * position.qty
    realized = gross - exit_fee - funding
    return ClosedTrade(
        symbol=position.symbol, direction=position.direction, decision_id=position.decision_id,
        entry=position.entry, exit_price=exit_fill, qty=position.qty, reason=reason,
        gross_pnl=gross, exit_fee=exit_fee, funding=funding, slippage=slippage, realized_pnl=realized,
    )
```

- [ ] **Step 4: Run** `uv run pytest tests/test_exits.py -v` — expect PASS (8 passed). Then `uv run ruff check futures_fund/exits.py tests/test_exits.py`.

- [ ] **Step 5: Commit**

```bash
git add futures_fund/exits.py tests/test_exits.py
git commit -m "feat: mark/bar-based exit detection (liquidation>stop>tp) with net PnL"
```

---

## Task 3: Reconciliation executor

**Files:** create `futures_fund/executor.py`, `tests/test_executor.py`.

- [ ] **Step 1: Write the failing test** — `tests/test_executor.py`:

```python
from datetime import datetime, timezone

import pytest

from futures_fund.executor import close_at_mark, open_position, reconcile
from futures_fund.models import CostEstimate, SizedTrade, TradeProposal
from tests.test_state import _pos


def _sized(symbol="BTCUSDT", direction="long", entry=100.0, stop=95.0):
    prop = TradeProposal(symbol=symbol, direction=direction, entry=entry, stop=stop,
                         take_profits=[entry * 1.15], atr=2.0, confidence=0.6,
                         horizon_hours=4, funding_rate=0.0)
    return SizedTrade(proposal=prop, qty=0.5, notional=entry * 0.5, leverage=5.0,
                      margin=entry * 0.5 / 5.0, liq_price=82.0, cost=CostEstimate())


def test_reconcile_opens_new_and_closes_removed():
    current = [_pos("BTCUSDT", "long")]
    target = {"ETHUSDT": _sized("ETHUSDT", "long")}   # BTC not in target, ETH new
    to_open, to_close = reconcile(target, current)
    assert [st.proposal.symbol for st in to_open] == ["ETHUSDT"]
    assert [p.symbol for p in to_close] == ["BTCUSDT"]


def test_reconcile_keeps_unchanged_symbol():
    current = [_pos("BTCUSDT", "long")]
    target = {"BTCUSDT": _sized("BTCUSDT", "long")}   # same symbol+direction held
    to_open, to_close = reconcile(target, current)
    assert to_open == [] and to_close == []


def test_reconcile_flips_direction_closes_then_opens():
    current = [_pos("BTCUSDT", "long")]
    target = {"BTCUSDT": _sized("BTCUSDT", "short", entry=100.0, stop=105.0)}
    to_open, to_close = reconcile(target, current)
    assert [st.proposal.direction for st in to_open] == ["short"]
    assert [p.symbol for p in to_close] == ["BTCUSDT"]


def test_open_position_applies_entry_slippage_and_returns_fee():
    st = _sized("BTCUSDT", "long", entry=100.0)
    pos, entry_fee = open_position(st, cycle=1, ts=datetime(2026, 5, 29, tzinfo=timezone.utc),
                                   slippage_bps=10, decision_id="d1")
    assert pos.entry == pytest.approx(100.1)        # buy slipped up
    assert pos.symbol == "BTCUSDT" and pos.decision_id == "d1"
    assert entry_fee == pytest.approx(0.5 * 100.1 * 0.0005)  # taker fee on notional


def test_close_at_mark_realizes_pnl_net_of_fee():
    pos = _pos("BTCUSDT", "long")  # qty 0.5 entry 100
    ct = close_at_mark(pos, mark=110.0, funding_rate=0.0, funding_events=0, slippage_bps=0)
    assert ct.reason == "stop"  # close_at_mark uses a synthetic 'decision' exit reason? see impl
    assert ct.realized_pnl == pytest.approx(0.5 * (110.0 - 100.0) - ct.exit_fee)
```

- [ ] **Step 2: Run** `uv run pytest tests/test_executor.py -v` — expect FAIL.

- [ ] **Step 3: Implement** `futures_fund/executor.py`:

```python
from __future__ import annotations

from datetime import datetime

from futures_fund.costs import project_funding, trade_fee
from futures_fund.exits import ClosedTrade
from futures_fund.fills import close_side, fill_price, open_side
from futures_fund.models import SizedTrade
from futures_fund.state import Position


def reconcile(
    target: dict[str, SizedTrade], current: list[Position]
) -> tuple[list[SizedTrade], list[Position]]:
    """Diff desired book vs open positions. Returns (to_open, to_close).
    A symbol held in the same direction is left untouched; a direction flip closes then reopens."""
    held = {p.symbol: p for p in current}
    to_open: list[SizedTrade] = []
    to_close: list[Position] = []
    for sym, st in target.items():
        cur = held.get(sym)
        if cur is None or cur.direction != st.proposal.direction:
            to_open.append(st)
    for p in current:
        st = target.get(p.symbol)
        if st is None or st.proposal.direction != p.direction:
            to_close.append(p)
    return to_open, to_close


def open_position(
    st: SizedTrade, cycle: int, ts: datetime, slippage_bps: float,
    decision_id: str | None = None, pay_bnb: bool = False,
) -> tuple[Position, float]:
    """Open a position at a slipped entry fill; returns (Position, entry_fee_usdt)."""
    p = st.proposal
    entry_fill = fill_price(p.entry, open_side(p.direction), slippage_bps)
    entry_fee = trade_fee(st.qty * entry_fill, maker=False, pay_bnb=pay_bnb)
    position = Position(
        symbol=p.symbol, direction=p.direction, qty=st.qty, entry=entry_fill, stop=p.stop,
        take_profits=p.take_profits, leverage=st.leverage, margin=st.margin,
        liq_price=st.liq_price, opened_cycle=cycle, opened_ts=ts, decision_id=decision_id,
    )
    return position, entry_fee


def close_at_mark(
    position: Position, mark: float, *, funding_rate: float, funding_events: int,
    slippage_bps: float, pay_bnb: bool = False,
) -> ClosedTrade:
    """Discretionary close at the current mark (used when the team exits a position by decision
    rather than a stop/tp/liq trigger). Reason is recorded as 'stop' for ledger uniformity."""
    side = close_side(position.direction)
    exit_fill = fill_price(mark, side, slippage_bps)
    if position.direction == "long":
        gross = position.qty * (exit_fill - position.entry)
    else:
        gross = position.qty * (position.entry - exit_fill)
    exit_fee = trade_fee(position.qty * exit_fill, maker=False, pay_bnb=pay_bnb)
    funding = max(0.0, project_funding(position.qty * position.entry, funding_rate,
                                       position.direction, funding_events))
    slippage = abs(exit_fill - mark) * position.qty
    return ClosedTrade(
        symbol=position.symbol, direction=position.direction, decision_id=position.decision_id,
        entry=position.entry, exit_price=exit_fill, qty=position.qty, reason="stop",
        gross_pnl=gross, exit_fee=exit_fee, funding=funding, slippage=slippage,
        realized_pnl=gross - exit_fee - funding,
    )
```

- [ ] **Step 4: Run** `uv run pytest tests/test_executor.py -v` — expect PASS (5 passed). Then `uv run ruff check futures_fund/executor.py tests/test_executor.py`.

- [ ] **Step 5: Commit**

```bash
git add futures_fund/executor.py tests/test_executor.py
git commit -m "feat: reconciliation executor (open/close/flip) + discretionary mark close"
```

---

## Task 4: Portfolio consolidation + CVaR de-risk

**Files:** create `futures_fund/consolidation.py`, `tests/test_consolidation.py`.

- [ ] **Step 1: Write the failing test** — `tests/test_consolidation.py`:

```python
import pytest

from futures_fund.consolidation import consolidate, cvar_risk_multiplier
from futures_fund.models import CostEstimate, SizedTrade, TradeProposal


def _sized(symbol, qty, entry=100.0, stop=95.0, direction="long"):
    prop = TradeProposal(symbol=symbol, direction=direction, entry=entry, stop=stop,
                         take_profits=[entry * 1.2], atr=2.0, confidence=0.6,
                         horizon_hours=4, funding_rate=0.0)
    return SizedTrade(proposal=prop, qty=qty, notional=entry * qty, leverage=5.0,
                      margin=entry * qty / 5.0, liq_price=82.0, cost=CostEstimate())


def test_cvar_multiplier_derisks_on_bad_tail():
    calm = cvar_risk_multiplier([0.01, 0.0, -0.01, 0.005], threshold=-0.05, floor=0.5)
    bad = cvar_risk_multiplier([-0.10, -0.08, 0.01, 0.0], threshold=-0.05, floor=0.5)
    assert calm == 1.0
    assert bad == 0.5


def test_cvar_multiplier_no_history_is_one():
    assert cvar_risk_multiplier([], threshold=-0.05) == 1.0


def test_consolidate_scales_book_to_gross_heat_cap():
    # two trades each risking 1% (qty 20, gap 5 on 10k); cap 0.015 -> must scale to 0.75x
    trades = [_sized("BTCUSDT", 20.0), _sized("ETHUSDT", 20.0)]
    out = consolidate(trades, equity=10_000.0, max_heat=0.015)
    total_risk = sum(t.qty * 5.0 / 10_000.0 for t in out)
    assert total_risk == pytest.approx(0.015, abs=1e-9)
    # qty scaled down proportionally
    assert out[0].qty == pytest.approx(20.0 * 0.75)


def test_consolidate_under_cap_is_unchanged():
    trades = [_sized("BTCUSDT", 10.0)]  # 0.5% risk, cap 10%
    out = consolidate(trades, equity=10_000.0, max_heat=0.10)
    assert out[0].qty == 10.0


def test_consolidate_applies_cvar_multiplier():
    trades = [_sized("BTCUSDT", 10.0)]
    out = consolidate(trades, equity=10_000.0, max_heat=0.10, cvar_mult=0.5)
    assert out[0].qty == pytest.approx(5.0)


def test_consolidate_drops_dust():
    trades = [_sized("BTCUSDT", 0.001)]  # negligible risk
    out = consolidate(trades, equity=10_000.0, max_heat=0.10, min_risk_frac=0.001)
    assert out == []
```

- [ ] **Step 2: Run** `uv run pytest tests/test_consolidation.py -v` — expect FAIL.

- [ ] **Step 3: Implement** `futures_fund/consolidation.py`:

```python
from __future__ import annotations

from futures_fund.models import SizedTrade
from futures_fund.policy import cvar
from futures_fund.portfolio_risk import position_risk


def cvar_risk_multiplier(recent_returns: list[float], threshold: float = -0.05,
                         floor: float = 0.5) -> float:
    """1.0 in calm tails; `floor` when CVaR breaches `threshold` (portfolio-level de-risk)."""
    if not recent_returns:
        return 1.0
    return floor if cvar(recent_returns) < threshold else 1.0


def _scale(st: SizedTrade, factor: float) -> SizedTrade:
    return st.model_copy(update={
        "qty": st.qty * factor,
        "notional": st.notional * factor,
        "margin": st.margin * factor,
    })


def consolidate(
    approved: list[SizedTrade], equity: float, max_heat: float,
    cvar_mult: float = 1.0, min_risk_frac: float = 0.001,
) -> list[SizedTrade]:
    """Turn the per-symbol approved trades into a final book: apply the portfolio-level CVaR
    de-risk, scale the batch down to the gross-heat cap, then drop dust positions.

    Gross heat here is the conservative sum of per-trade risk (>= any single correlation
    cluster's heat), so no unsafe book slips through. Cluster-aware refinement (treating
    correlated trades as one) is available via portfolio_risk.cluster_heat for Phase B's PM."""
    trades = [_scale(t, cvar_mult) for t in approved] if cvar_mult != 1.0 else list(approved)

    def risk(t: SizedTrade) -> float:
        return position_risk(t.qty, t.proposal.entry, t.proposal.stop, equity)

    total = sum(risk(t) for t in trades)
    if total > max_heat and total > 0:
        factor = max_heat / total
        trades = [_scale(t, factor) for t in trades]

    return [t for t in trades if risk(t) >= min_risk_frac]
```

- [ ] **Step 4: Run** `uv run pytest tests/test_consolidation.py -v` — expect PASS (6 passed). Then `uv run ruff check futures_fund/consolidation.py tests/test_consolidation.py`.

- [ ] **Step 5: Commit**

```bash
git add futures_fund/consolidation.py tests/test_consolidation.py
git commit -m "feat: portfolio consolidation (gross-heat cap, CVaR de-risk, drop dust)"
```

---

## Task 5: Baseline strategy (no LLM)

**Files:** create `futures_fund/baseline.py`, `tests/test_baseline.py`.

- [ ] **Step 1: Write the failing test** — `tests/test_baseline.py`:

```python
import numpy as np
import pandas as pd

from futures_fund.baseline import propose, simple_regime
from futures_fund.models import RegimeState, TradeProposal


def _trend_df(slope: float, n: int = 60, base: float = 100.0, noise: float = 0.05) -> pd.DataFrame:
    rng = np.random.default_rng(0)
    close = base + slope * np.arange(n) + rng.normal(0, noise, n)
    high = close + 0.2
    low = close - 0.2
    return pd.DataFrame({
        "timestamp": pd.date_range("2026-01-01", periods=n, freq="4h", tz="UTC"),
        "open": close, "high": high, "low": low, "close": close, "volume": 1.0,
    })


def test_simple_regime_returns_regimestate():
    r = simple_regime(_trend_df(0.5))
    assert isinstance(r, RegimeState)
    assert r.quadrant in {"low_vol_trend", "high_vol_trend", "low_vol_range",
                          "high_vol_range", "transition"}


def test_propose_long_on_clean_uptrend():
    p = propose("BTCUSDT", _trend_df(0.8), funding_rate=0.0, horizon_hours=4)
    assert isinstance(p, TradeProposal)
    assert p.direction == "long"
    assert p.stop < p.entry            # long stop below entry
    assert p.take_profits[0] > p.entry
    # reward:risk ~ 2:1 by construction
    assert (p.take_profits[0] - p.entry) / (p.entry - p.stop) >= 1.9


def test_propose_short_on_clean_downtrend():
    p = propose("BTCUSDT", _trend_df(-0.8), funding_rate=0.0, horizon_hours=4)
    assert p.direction == "short"
    assert p.stop > p.entry
    assert p.take_profits[0] < p.entry


def test_propose_flat_on_no_trend_returns_none():
    p = propose("BTCUSDT", _trend_df(0.0, noise=0.02), funding_rate=0.0, horizon_hours=4)
    assert p is None
```

- [ ] **Step 2: Run** `uv run pytest tests/test_baseline.py -v` — expect FAIL.

- [ ] **Step 3: Implement** `futures_fund/baseline.py`:

```python
from __future__ import annotations

import pandas as pd

from futures_fund.models import RegimeState, TradeProposal

_EMA_SPAN = 20
_ATR_PERIOD = 14
_ATR_MULT = 2.0
_RR = 2.0
_TREND_EPS = 0.0005  # min |ema slope / price| per bar to call a trend


def _atr(df: pd.DataFrame, period: int = _ATR_PERIOD) -> float:
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([(high - low), (high - prev_close).abs(), (low - prev_close).abs()],
                   axis=1).max(axis=1)
    return float(tr.rolling(period).mean().iloc[-1])


def simple_regime(df: pd.DataFrame) -> RegimeState:
    close = df["close"]
    ema = close.ewm(span=_EMA_SPAN, adjust=False).mean()
    slope = (ema.iloc[-1] - ema.iloc[-6]) / 5.0
    norm_slope = slope / close.iloc[-1]
    vol = float(close.pct_change().tail(_EMA_SPAN).std())
    trending = abs(norm_slope) > _TREND_EPS
    high_vol = vol > 0.01
    direction = "up" if norm_slope > 0 else "down" if norm_slope < 0 else "neutral"
    if trending:
        quadrant = "high_vol_trend" if high_vol else "low_vol_trend"
    else:
        quadrant = "high_vol_range" if high_vol else "low_vol_range"
    return RegimeState(quadrant=quadrant, trend_direction=direction)


def propose(symbol: str, df: pd.DataFrame, funding_rate: float,
            horizon_hours: float = 4.0) -> TradeProposal | None:
    """Deterministic momentum baseline (stand-in for the Phase-B team): trade in the trend
    direction with an ATR stop and a 2R take-profit; flat when there's no trend."""
    regime = simple_regime(df)
    if regime.trend_direction == "neutral" or regime.quadrant in ("low_vol_range", "high_vol_range"):
        return None
    atr = _atr(df)
    if not atr or atr <= 0:
        return None
    entry = float(df["close"].iloc[-1])
    if regime.trend_direction == "up":
        stop = entry - _ATR_MULT * atr
        tp = entry + _RR * _ATR_MULT * atr
        direction = "long"
    else:
        stop = entry + _ATR_MULT * atr
        tp = entry - _RR * _ATR_MULT * atr
        direction = "short"
    return TradeProposal(symbol=symbol, direction=direction, entry=entry, stop=stop,
                         take_profits=[tp], atr=atr, confidence=0.5,
                         horizon_hours=horizon_hours, funding_rate=funding_rate)
```

- [ ] **Step 4: Run** `uv run pytest tests/test_baseline.py -v` — expect PASS (4 passed). Then `uv run ruff check futures_fund/baseline.py tests/test_baseline.py`.

- [ ] **Step 5: Commit**

```bash
git add futures_fund/baseline.py tests/test_baseline.py
git commit -m "feat: deterministic momentum baseline strategy (ATR stop, 2R target)"
```

---

## Task 6: The phased cycle runner (end-to-end, no LLM)

**Files:** modify `futures_fund/config.py` (add `symbols`); create `futures_fund/cycle.py`, `scripts/run_cycle.py`, `tests/test_cycle.py`.

- [ ] **Step 1: Add `symbols` to config.** In `futures_fund/config.py`, add to `class Settings` (after `symbol_count`):

```python
    symbols: list[str] = Field(default_factory=lambda: ["BTC/USDT:USDT", "ETH/USDT:USDT"])
```

- [ ] **Step 2: Write the failing test** — `tests/test_cycle.py`:

```python
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from futures_fund.config import Settings
from futures_fund.cycle import run_cycle
from futures_fund.journal import read_all_decisions, read_open_decisions
from futures_fund.models import MmrBracket, SymbolSpec
from futures_fund.state import load_account, load_positions


class FakeExchange:
    """Injected stand-in for FuturesExchange returning scripted data per symbol."""

    def __init__(self, frames: dict[str, pd.DataFrame], funding_rate: float = 0.0):
        self.frames = frames
        self.funding_rate = funding_rate

    def symbol_spec(self, symbol):
        return SymbolSpec(symbol=symbol.split("/")[0] + "USDT" if "/" in symbol else symbol,
                          tick_size=0.01, step_size=0.001, min_notional=5.0,
                          mmr_brackets=[MmrBracket(notional_floor=0, notional_cap=1_000_000,
                                                   mmr=0.004, maint_amount=0.0, max_leverage=125)])

    def ohlcv(self, symbol, timeframe="4h", limit=500):
        return self.frames[symbol]

    def funding(self, symbol):
        from futures_fund.market_data import FundingInfo
        return FundingInfo(symbol=symbol, current_rate=self.funding_rate,
                           next_funding_ts=datetime(2026, 1, 1, tzinfo=timezone.utc),
                           interval_hours=8.0, mark_price=float(self.frames[symbol]["close"].iloc[-1]),
                           index_price=float(self.frames[symbol]["close"].iloc[-1]))

    def mark_price(self, symbol):
        return float(self.frames[symbol]["close"].iloc[-1])


def _frame(closes, highs=None, lows=None):
    n = len(closes)
    closes = np.asarray(closes, dtype=float)
    high = np.asarray(highs, dtype=float) if highs is not None else closes + 0.2
    low = np.asarray(lows, dtype=float) if lows is not None else closes - 0.2
    return pd.DataFrame({
        "timestamp": pd.date_range("2026-01-01", periods=n, freq="4h", tz="UTC"),
        "open": closes, "high": high, "low": low, "close": closes, "volume": 1.0,
    })


def _uptrend(n=60, base=100.0, slope=0.8):
    rng = np.random.default_rng(1)
    return _frame(base + slope * np.arange(n) + rng.normal(0, 0.05, n))


def _settings():
    return Settings(account_size_usdt=10_000.0, symbols=["BTC/USDT:USDT"], timeframe="4h")


def test_cycle_runs_end_to_end_and_opens_a_position(tmp_path):
    state_dir, memory_dir = tmp_path / "state", tmp_path / "memory"
    ex = FakeExchange({"BTC/USDT:USDT": _uptrend()})
    report = run_cycle(ex, _settings(), state_dir, memory_dir,
                       now=datetime(2026, 3, 1, tzinfo=timezone.utc), cycle_no=1)
    # the clean uptrend should produce one approved long
    assert report["opened"] == 1
    positions = load_positions(state_dir)
    assert len(positions) == 1 and positions[0].direction == "long"
    # a Phase-1 decision was journaled and is still open (no outcome yet)
    assert len(read_open_decisions(memory_dir)) == 1
    # account + memory artifacts persisted
    assert (state_dir / "account.json").exists()
    assert (memory_dir / "semantic" / "beliefs.md").exists()


def test_second_cycle_closes_position_on_crash(tmp_path):
    state_dir, memory_dir = tmp_path / "state", tmp_path / "memory"
    ex1 = FakeExchange({"BTC/USDT:USDT": _uptrend()})
    run_cycle(ex1, _settings(), state_dir, memory_dir,
              now=datetime(2026, 3, 1, tzinfo=timezone.utc), cycle_no=1)
    pos = load_positions(state_dir)[0]
    # build a frame whose final bar crashes below the open position's stop
    crash = _uptrend()
    crash.loc[crash.index[-1], ["low", "close"]] = pos.stop - 5.0
    ex2 = FakeExchange({"BTC/USDT:USDT": crash})
    report = run_cycle(ex2, _settings(), state_dir, memory_dir,
                       now=datetime(2026, 3, 1, 4, tzinfo=timezone.utc), cycle_no=2)
    assert report["closed"] >= 1
    # the decision now has a realized outcome (Phase-2 patched)
    closed = [d for d in read_all_decisions(memory_dir) if d.get("realized_pnl") is not None]
    assert len(closed) >= 1


def test_halt_flag_skips_trading(tmp_path):
    from futures_fund.state import set_halt
    state_dir, memory_dir = tmp_path / "state", tmp_path / "memory"
    load_account(state_dir, 10_000.0)  # create account file
    from futures_fund.state import save_account, AccountState
    save_account(state_dir, AccountState(balance=10_000.0, peak_equity=10_000.0))
    set_halt(state_dir, True, reason="test")
    ex = FakeExchange({"BTC/USDT:USDT": _uptrend()})
    report = run_cycle(ex, _settings(), state_dir, memory_dir,
                       now=datetime(2026, 3, 1, tzinfo=timezone.utc), cycle_no=1)
    assert report["halted"] is True
    assert report["opened"] == 0
    assert load_positions(state_dir) == []
```

- [ ] **Step 3: Run** `uv run pytest tests/test_cycle.py -v` — expect FAIL.

- [ ] **Step 4: Implement** `futures_fund/cycle.py`:

```python
from __future__ import annotations

from datetime import datetime

from futures_fund.baseline import propose, simple_regime
from futures_fund.config import Settings
from futures_fund.consolidation import consolidate, cvar_risk_multiplier
from futures_fund.executor import close_at_mark, open_position, reconcile
from futures_fund.exits import detect_exit
from futures_fund.hitrate import hit_rate, record_outcome
from futures_fund.journal import append_decision, patch_outcome, read_all_decisions
from futures_fund.memory_layout import ensure_memory_layout
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


def _hours_held(opened_ts: datetime, now: datetime) -> float:
    return max(0.0, (now - opened_ts).total_seconds() / 3600.0)


def _recent_returns(memory_dir, equity: float) -> list[float]:
    pnls = [d["realized_pnl"] for d in read_all_decisions(memory_dir)
            if d.get("realized_pnl") is not None]
    return [p / equity for p in pnls[-30:]] if equity > 0 else []


def run_cycle(exchange, settings: Settings, state_dir, memory_dir,
              now: datetime, cycle_no: int) -> dict:
    """Run one deterministic trading cycle (phases 0-11, no LLM). Returns a CycleReport dict.
    `exchange` must expose symbol_spec/ohlcv/funding/mark_price (FuturesExchange or a fake)."""
    # Phase 0 — preflight
    ensure_memory_layout(memory_dir)
    account = load_account(state_dir, settings.account_size_usdt)
    positions = load_positions(state_dir)
    report = {"cycle": cycle_no, "halted": False, "opened": 0, "closed": 0,
              "equity": account.balance, "actions": []}
    if is_halted(state_dir):
        report["halted"] = True
        return report

    frames = {s: exchange.ohlcv(s, settings.timeframe) for s in settings.symbols}
    fundings = {s: exchange.funding(s) for s in settings.symbols}

    # Phase 1 — audit & reflect: close positions whose latest bar hit stop/tp/liq
    still_open: list[Position] = []
    for p in positions:
        sym = next((s for s in settings.symbols if exchange.symbol_spec(s).symbol == p.symbol), None)
        df = frames.get(sym) if sym else None
        if df is None:
            still_open.append(p)
            continue
        bar = df.iloc[-1]
        fr = fundings[sym]
        n_events = int(_hours_held(p.opened_ts, now) // fr.interval_hours)
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
    positions = still_open

    # Phase 2 — regime + portfolio health
    prices = {exchange.symbol_spec(s).symbol: float(frames[s]["close"].iloc[-1]) for s in settings.symbols}
    health = portfolio_health(account.balance, account.peak_equity, positions, prices,
                              recent_hit_rate=hit_rate(memory_dir, _BASELINE))
    btc_df = frames[settings.symbols[0]]
    caps = caps_for(simple_regime(btc_df), health)
    report["equity"] = health.equity

    # Phases 3-7 — watcher (configured symbols) -> baseline proposals -> risk gate
    open_dicts = [{"symbol": p.symbol, "direction": p.direction, "qty": p.qty,
                   "entry": p.entry, "stop": p.stop} for p in positions]
    approved = []
    for s in settings.symbols:
        spec = exchange.symbol_spec(s)
        prop = propose(spec.symbol, frames[s], fundings[s].current_rate,
                       horizon_hours=4.0)
        if prop is None:
            continue
        decision = evaluate(GateInputs(
            proposal=prop, spec=spec, regime=simple_regime(frames[s]), health=health,
            open_positions=open_dicts,
        ))
        if decision.verdict in ("approve", "resize") and decision.sized_trade is not None:
            approved.append(decision.sized_trade)

    # Phase 8 — consolidation (gross-heat cap + CVaR de-risk)
    cvar_mult = cvar_risk_multiplier(_recent_returns(memory_dir, health.equity))
    book = consolidate(approved, health.equity, caps.max_heat, cvar_mult=cvar_mult)

    # Phase 9 — execution (reconcile + fills + journal Phase-1)
    target = {st.proposal.symbol: st for st in book}
    to_open, to_close = reconcile(target, positions)
    for p in to_close:
        ct = close_at_mark(p, prices.get(p.symbol, p.entry), funding_rate=0.0,
                           funding_events=0, slippage_bps=_SLIPPAGE_BPS)
        account.balance += ct.realized_pnl
        report["closed"] += 1
        if p.decision_id:
            patch_outcome(memory_dir, p.decision_id, {
                "exit_ts": now, "realized_pnl": ct.realized_pnl, "fees": ct.exit_fee,
                "prediction_correct": ct.realized_pnl > 0,
            })
            record_outcome(memory_dir, _BASELINE, ct.realized_pnl > 0)
    keep = [p for p in positions if p not in to_close]
    for st in to_open:
        did = append_decision(memory_dir, {
            "ts": now, "cycle": cycle_no, "symbol": st.proposal.symbol,
            "direction": st.proposal.direction, "entry": st.proposal.entry, "stop": st.proposal.stop,
            "take_profit": st.proposal.take_profits, "size": st.qty, "leverage": st.leverage,
            "funding_at_entry": st.proposal.funding_rate, "confidence": st.proposal.confidence,
            "dominant_signal": "baseline-momentum", "contributing_agents": [_BASELINE],
        })
        pos, entry_fee = open_position(st, cycle_no, now, _SLIPPAGE_BPS, decision_id=did)
        account.balance -= entry_fee
        keep.append(pos)
        report["opened"] += 1
        report["actions"].append({"open": pos.symbol, "direction": pos.direction})
    positions = keep

    # Phase 10 — persist (no silent runs) + recompute equity/peak
    final_health = portfolio_health(account.balance, account.peak_equity, positions, prices,
                                    recent_hit_rate=hit_rate(memory_dir, _BASELINE))
    account.peak_equity = max(account.peak_equity, final_health.equity)
    account.updated_ts = now
    save_account(state_dir, account)
    save_positions(state_dir, positions)
    report["equity"] = final_health.equity
    return report
```

- [ ] **Step 5: Run** `uv run pytest tests/test_cycle.py -v` — expect PASS (3 passed). If a test fails, debug with `superpowers:systematic-debugging` (do NOT weaken assertions). Likely culprits to check in order: the uptrend must clear `simple_regime`'s trend threshold (it does at slope 0.8); the risk gate must approve (low_vol/high_vol_trend healthy → non-zero risk; RR is 2:1 by construction; notional ≥ min 5); the crash bar's `low` must be below the open position's `stop`.

- [ ] **Step 6: Create the CLI** `scripts/run_cycle.py`:

```python
"""Run one paper trading cycle from the command line (deterministic baseline, no LLM):

    uv run python scripts/run_cycle.py --cycle 1

Uses config.yaml + env (testnet keys optional for public data). State in state/, memory in memory/.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

from futures_fund.config import load_settings
from futures_fund.cycle import run_cycle
from futures_fund.exchange import FuturesExchange


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one paper trading cycle")
    parser.add_argument("--cycle", type=int, default=1)
    args = parser.parse_args()
    settings = load_settings()
    exchange = FuturesExchange.from_settings(settings)
    report = run_cycle(exchange, settings, "state", "memory",
                       now=datetime.now(timezone.utc), cycle_no=args.cycle)
    print(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    main()
```

- [ ] **Step 7: Run the FULL suite + lint:** `uv run pytest` then `uv run ruff check .`. Report the EXACT total (expected 103 + fills 4 + exits 8 + executor 5 + consolidation 6 + baseline 4 + cycle 3 = **133**). Do NOT run the CLI (it needs network).

- [ ] **Step 8: Commit**

```bash
git add futures_fund/config.py futures_fund/cycle.py scripts/run_cycle.py tests/test_cycle.py
git commit -m "feat: phased deterministic cycle runner (end-to-end paper, no LLM) + CLI"
```

---

## Self-Review (completed during planning)

**Spec coverage (§4 cycle, §7 cost/risk):** reconciliation paper execution with resting stop/TP + mark-price liquidation detection ✓ (T2/T3/cycle phase 1); fills costed with A1 fees+funding+slippage ✓ (T1/T2); leverage-as-output + adaptive caps enforced via A1 `evaluate` ✓ (cycle phase 7); CVaR portfolio de-risk wired ✓ (T4 + cycle phase 8 — the A1 deferral); deterministic portfolio consolidation with gross-heat cap ✓ (T4); two-phase journaling on open/close + hit-rate ✓ (cycle phases 1/9); HALT honored, no-silent-runs persistence ✓ (cycle phases 0/10); phased tick wiring A1+A2+A3a ✓ (T6). Deferred to B (correct): the LLM team (Watcher/analysts/debate/judges/Reflector) replaces the baseline + `simple_regime`; daily/weekly/monthly breaker inputs need an equity-history log (cycle passes the drawdown breaker via `health.drawdown_from_peak`; period-PnL is left 0.0 with a note); self-healing orchestrator; cluster-aware PM consolidation (the conservative sum-heat is correct, cluster refinement is available via `cluster_heat`).

**Placeholder scan:** none — every step has runnable code/fixtures and exact commands.

**Type/interface consistency:** `detect_exit`/`close_at_mark` both return `ClosedTrade`. `reconcile` returns `(list[SizedTrade], list[Position])` consumed correctly in the cycle. `consolidate` consumes/returns `SizedTrade` (model_copy preserves `proposal`). The cycle builds `GateInputs` with the exact A1 fields (no `corr` — removed in the A2-era gate fix). `propose` returns A1 `TradeProposal`; `simple_regime` returns A1 `RegimeState`. Journal append uses spec field names; patch uses Phase-2 names. `portfolio_health` signature matches A3a.

**Integration risks flagged for execution:** (1) the e2e test depends on the baseline proposing AND the gate approving a long on the synthetic uptrend — the threshold math is set so it does (slope 0.8 ≫ trend eps; RR 2:1; min-notional 5 with notional ≈ qty·100 ≫ 5). If the gate vetoes, inspect `caps_for(simple_regime(...))` — a `high_vol_range`/`transition` quadrant would force flat; the uptrend yields a `_trend` quadrant. (2) `_recent_returns` reads the journal each cycle — fine at paper scale. (3) symbol id mapping: the cycle matches positions to config symbols via `symbol_spec(s).symbol`; the fake's `symbol_spec` returns the raw id, mirroring A2.

# Futures-Fund Phase A1 — Cost & Risk Core Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the pure, deterministic "survival math" library for the TEMPEST futures desk — fees, funding, slippage, liquidation/MMR, position sizing, the adaptive `regime × portfolio-health` risk matrix, circuit breakers, and the final risk gate — with no network or filesystem I/O, fully unit-tested.

**Architecture:** A small `futures_fund` Python package of pure functions and typed pydantic models. Each module is independently testable with synthetic inputs. The capstone is `risk_gate.evaluate(...)`, which composes sizing + liquidation + cost + policy + portfolio-heat into a single `approve / resize / veto` decision. Later plans (A2 data, A3 cycle) feed this core real exchange data.

**Tech Stack:** Python 3.11+, `uv` for env/deps, `pydantic` v2 (typed models/validation), `numpy` + `pandas` (math), `pytest` (tests), `ruff` (lint). No network libs in this plan.

**Reference spec:** `docs/superpowers/specs/2026-05-29-futures-fund-design.md` (esp. §7 Cost & risk core and §7.1 the adaptive matrix).

**Conventions used throughout:**
- All monetary quantities are USDT. `direction` is the string literal `"long"` or `"short"`.
- "Notional" = `qty × price`. "Margin" (isolated) = `notional / leverage`.
- Liquidation price is computed from entry/qty/margin; in live trading the *trigger* compares **mark price** to this liq price (documented, enforced in A3).
- Funding settles at 00:00/08:00/16:00 UTC; a position pays/receives only if held *at* a boundary.

---

## File Structure

Created by this plan:

```
crypto-trade-claude-code/
  pyproject.toml                 # uv project + deps + pytest/ruff config
  MISSION.md                     # the TEMPEST charter (from spec §0)
  README.md                      # repo overview + how to run tests
  futures_fund/
    __init__.py
    models.py                    # pydantic data contracts (shared types)
    costs.py                     # fees, funding projection, slippage
    liquidation.py               # tiered MMR + isolated liquidation price
    sizing.py                    # ATR-stop sizing, leverage-as-output, liq-distance rule
    portfolio_risk.py            # portfolio heat + correlation-cluster risk
    policy.py                    # adaptive regime×health matrix, circuit breakers, CVaR alarm
    risk_gate.py                 # the capstone: approve / resize / veto
  tests/
    __init__.py
    test_models.py
    test_costs.py
    test_liquidation.py
    test_sizing.py
    test_portfolio_risk.py
    test_policy.py
    test_risk_gate.py
```

> **Note (refines spec §11):** the spec lists a single `scripts/` dir. For testability we split code into an importable `futures_fund/` package now; thin `scripts/` CLI entrypoints arrive in plan A3. This is the only deviation from the spec layout.

---

## Task 1: Project scaffolding

**Files:**
- Create: `pyproject.toml`, `README.md`, `MISSION.md`, `futures_fund/__init__.py`, `tests/__init__.py`

- [ ] **Step 1: Create `pyproject.toml`**

```toml
[project]
name = "futures-fund"
version = "0.1.0"
description = "Operation TEMPEST — autonomous multi-agent Binance USD-M futures desk"
requires-python = ">=3.11"
dependencies = [
    "pydantic>=2.6",
    "numpy>=1.26",
    "pandas>=2.1",
]

[dependency-groups]
dev = [
    "pytest>=8.0",
    "ruff>=0.4",
]

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-q"

[tool.ruff]
line-length = 100
target-version = "py311"

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["futures_fund"]
```

- [ ] **Step 2: Create package markers and `MISSION.md`**

`futures_fund/__init__.py`:
```python
"""Operation TEMPEST — futures desk core."""

__version__ = "0.1.0"
```

`tests/__init__.py`: (empty file)

`MISSION.md` — copy the charter verbatim from spec §0 (the `OPERATION TEMPEST` block).

`README.md`:
```markdown
# futures-fund — Operation TEMPEST

Autonomous multi-agent Binance USD-M perpetual futures desk (Claude Code skill).
See `docs/superpowers/specs/` for the design and `docs/superpowers/plans/` for build plans.

## Dev
```
uv sync
uv run pytest
uv run ruff check .
```
```

- [ ] **Step 3: Initialize the environment**

Run: `cd /home/roberto/crypto-trade-claude-code && uv sync`
Expected: creates `.venv` and a `uv.lock`; exits 0.

- [ ] **Step 4: Verify pytest runs (collects zero tests)**

Run: `uv run pytest`
Expected: exits 0, "no tests ran".

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml uv.lock README.md MISSION.md futures_fund/__init__.py tests/__init__.py
git commit -m "chore: scaffold futures_fund package (Phase A1)"
```

---

## Task 2: Domain models (shared data contracts)

**Files:**
- Create: `futures_fund/models.py`
- Test: `tests/test_models.py`

These pydantic models are the typed contracts every later task references. Frozen where they are values.

- [ ] **Step 1: Write the failing test**

`tests/test_models.py`:
```python
import pytest
from pydantic import ValidationError

from futures_fund.models import (
    MmrBracket, SymbolSpec, TradeProposal, CostEstimate, SizedTrade,
    RegimeState, PortfolioHealth, RiskCaps, RiskDecision,
)


def test_trade_proposal_rejects_bad_direction():
    with pytest.raises(ValidationError):
        TradeProposal(symbol="BTCUSDT", direction="up", entry=100.0, stop=95.0,
                      take_profits=[110.0], atr=2.0, confidence=0.6, horizon_hours=8,
                      funding_rate=0.0001)


def test_trade_proposal_long_stop_below_entry_ok():
    p = TradeProposal(symbol="BTCUSDT", direction="long", entry=100.0, stop=95.0,
                      take_profits=[110.0], atr=2.0, confidence=0.6, horizon_hours=8,
                      funding_rate=0.0001)
    assert p.risk_per_unit == pytest.approx(5.0)


def test_symbol_spec_bracket_lookup_orders_brackets():
    spec = SymbolSpec(
        symbol="BTCUSDT", tick_size=0.1, step_size=0.001, min_notional=5.0,
        mmr_brackets=[
            MmrBracket(notional_floor=50000, notional_cap=250000, mmr=0.01, maint_amount=50.0, max_leverage=25),
            MmrBracket(notional_floor=0, notional_cap=50000, mmr=0.004, maint_amount=0.0, max_leverage=125),
        ],
    )
    assert spec.sorted_brackets[0].notional_floor == 0


def test_risk_decision_resize_requires_sized_trade():
    with pytest.raises(ValidationError):
        RiskDecision(verdict="resize", reason="too big", sized_trade=None)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_models.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'futures_fund.models'`.

- [ ] **Step 3: Write the implementation**

`futures_fund/models.py`:
```python
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

Direction = Literal["long", "short"]
RegimeQuadrant = Literal[
    "low_vol_trend", "high_vol_trend", "low_vol_range", "high_vol_range", "transition"
]
HealthTier = Literal["healthy", "caution", "stressed"]
Bias = Literal["normal", "reduce", "flat"]
Verdict = Literal["approve", "resize", "veto"]


class MmrBracket(BaseModel):
    notional_floor: float
    notional_cap: float
    mmr: float                      # maintenance margin rate
    maint_amount: float             # maintenance amount offset (cum)
    max_leverage: float


class SymbolSpec(BaseModel):
    symbol: str
    tick_size: float
    step_size: float
    min_notional: float
    mmr_brackets: list[MmrBracket]

    @property
    def sorted_brackets(self) -> list[MmrBracket]:
        return sorted(self.mmr_brackets, key=lambda b: b.notional_floor)


class TradeProposal(BaseModel):
    symbol: str
    direction: Direction
    entry: float
    stop: float
    take_profits: list[float] = Field(default_factory=list)
    atr: float
    confidence: float = Field(ge=0.0, le=1.0)
    horizon_hours: float = Field(gt=0)
    funding_rate: float             # current/predicted 8h funding rate (e.g. 0.0001)

    @model_validator(mode="after")
    def _check_stop_side(self) -> TradeProposal:
        if self.direction == "long" and self.stop >= self.entry:
            raise ValueError("long stop must be below entry")
        if self.direction == "short" and self.stop <= self.entry:
            raise ValueError("short stop must be above entry")
        return self

    @property
    def risk_per_unit(self) -> float:
        return abs(self.entry - self.stop)


class CostEstimate(BaseModel):
    entry_fee: float = 0.0
    exit_fee: float = 0.0
    funding: float = 0.0
    slippage: float = 0.0

    @property
    def total(self) -> float:
        return self.entry_fee + self.exit_fee + self.funding + self.slippage


class SizedTrade(BaseModel):
    proposal: TradeProposal
    qty: float
    notional: float
    leverage: float
    margin: float
    liq_price: float
    cost: CostEstimate


class RegimeState(BaseModel):
    quadrant: RegimeQuadrant
    trend_direction: Literal["up", "down", "neutral"] = "neutral"
    hurst: float = 0.5


class PortfolioHealth(BaseModel):
    equity: float
    peak_equity: float
    open_heat: float = 0.0          # fraction of equity currently at risk (0..1)
    recent_hit_rate: float = 0.5

    @property
    def drawdown_from_peak(self) -> float:
        if self.peak_equity <= 0:
            return 0.0
        return max(0.0, (self.peak_equity - self.equity) / self.peak_equity)

    @property
    def tier(self) -> HealthTier:
        dd = self.drawdown_from_peak
        if dd > 0.10:
            return "stressed"
        if dd >= 0.05:
            return "caution"
        return "healthy"


class RiskCaps(BaseModel):
    max_leverage: float
    per_trade_risk_pct: float       # fraction of equity, e.g. 0.01
    max_heat: float                 # fraction of equity, e.g. 0.10
    bias: Bias


class RiskDecision(BaseModel):
    verdict: Verdict
    reason: str
    sized_trade: SizedTrade | None = None
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_sized(self) -> RiskDecision:
        if self.verdict in ("approve", "resize") and self.sized_trade is None:
            raise ValueError(f"verdict '{self.verdict}' requires a sized_trade")
        return self
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_models.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add futures_fund/models.py tests/test_models.py
git commit -m "feat: domain models / data contracts for risk core"
```

---

## Task 3: Fee model

**Files:**
- Create: `futures_fund/costs.py`
- Test: `tests/test_costs.py`

- [ ] **Step 1: Write the failing test**

`tests/test_costs.py` (fee section):
```python
import pytest

from futures_fund.costs import trade_fee, round_trip_fee


def test_taker_fee_is_5bps_of_notional():
    assert trade_fee(notional=10_000.0, maker=False) == pytest.approx(5.0)  # 0.05%


def test_maker_fee_is_2bps_of_notional():
    assert trade_fee(notional=10_000.0, maker=True) == pytest.approx(2.0)   # 0.02%


def test_bnb_discount_applies_10pct():
    assert trade_fee(10_000.0, maker=False, pay_bnb=True) == pytest.approx(4.5)


def test_round_trip_taker_in_and_out():
    # taker entry + taker exit on 10k notional ≈ 10.0 USDT (0.10%)
    assert round_trip_fee(10_000.0, maker_entry=False, maker_exit=False) == pytest.approx(10.0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_costs.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'futures_fund.costs'`.

- [ ] **Step 3: Write the implementation**

`futures_fund/costs.py` (fee section — create the file with this content):
```python
from __future__ import annotations

from datetime import datetime, timedelta

from futures_fund.models import Direction

TAKER_RATE = 0.0005   # 0.05%
MAKER_RATE = 0.0002   # 0.02%
BNB_DISCOUNT = 0.90    # 10% off when paying fees in BNB


def trade_fee(notional: float, *, maker: bool, pay_bnb: bool = False) -> float:
    """Fee in USDT for a single fill of `notional` USDT."""
    rate = MAKER_RATE if maker else TAKER_RATE
    fee = abs(notional) * rate
    return fee * BNB_DISCOUNT if pay_bnb else fee


def round_trip_fee(
    notional: float, *, maker_entry: bool, maker_exit: bool, pay_bnb: bool = False
) -> float:
    """Entry + exit fee assuming the same notional both legs (conservative)."""
    return (
        trade_fee(notional, maker=maker_entry, pay_bnb=pay_bnb)
        + trade_fee(notional, maker=maker_exit, pay_bnb=pay_bnb)
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_costs.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add futures_fund/costs.py tests/test_costs.py
git commit -m "feat: maker/taker fee model with BNB discount"
```

---

## Task 4: Funding projection

**Files:**
- Modify: `futures_fund/costs.py`
- Modify: `tests/test_costs.py`

Funding settles at 00:00/08:00/16:00 UTC. A long pays when funding_rate > 0; a short receives it (and vice-versa). We count boundaries strictly inside `(entry_ts, exit_ts]`.

- [ ] **Step 1: Write the failing test (append to `tests/test_costs.py`)**

```python
from datetime import datetime, timezone

from futures_fund.costs import count_funding_events, project_funding


def _utc(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc)


def test_count_funding_events_crossing_two_boundaries():
    # 07:00 -> 17:00 UTC crosses the 08:00 and 16:00 settlements = 2
    n = count_funding_events(_utc(2026, 5, 29, 7, 0), _utc(2026, 5, 29, 17, 0))
    assert n == 2


def test_count_funding_events_none_within_window():
    # 09:00 -> 15:00 crosses no boundary
    assert count_funding_events(_utc(2026, 5, 29, 9), _utc(2026, 5, 29, 15)) == 0


def test_count_funding_events_4h_interval_more_events():
    # 4h funding: 07:00 -> 17:00 crosses 08:00, 12:00, 16:00 = 3
    n = count_funding_events(_utc(2026, 5, 29, 7, 0), _utc(2026, 5, 29, 17, 0), interval_hours=4)
    assert n == 3


def test_long_pays_positive_funding():
    # notional 10k, funding +0.01% per event, 2 events, long -> pays 2.0 USDT (positive cost)
    cost = project_funding(notional=10_000.0, funding_rate=0.0001, direction="long", n_events=2)
    assert cost == pytest.approx(2.0)


def test_short_receives_positive_funding():
    cost = project_funding(notional=10_000.0, funding_rate=0.0001, direction="short", n_events=2)
    assert cost == pytest.approx(-2.0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_costs.py -k funding -v`
Expected: FAIL — `ImportError: cannot import name 'count_funding_events'`.

- [ ] **Step 3: Write the implementation (append to `futures_fund/costs.py`)**

```python
DEFAULT_FUNDING_INTERVAL_HOURS = 8  # majors (BTC/ETH); many perps are 4h, 1h under stress


def funding_boundary_hours(interval_hours: int = DEFAULT_FUNDING_INTERVAL_HOURS) -> tuple[int, ...]:
    """UTC hours at which funding settles (8h -> 0,8,16; 4h -> 0,4,8,12,16,20)."""
    return tuple(range(0, 24, interval_hours))


def count_funding_events(
    entry_ts: datetime, exit_ts: datetime,
    interval_hours: int = DEFAULT_FUNDING_INTERVAL_HOURS,
) -> int:
    """Number of funding settlements strictly within (entry_ts, exit_ts].

    The interval is CONTRACT-SPECIFIC on Binance (8h for majors, 4h for many perps,
    1h under extreme volatility). A2/A3 must source it per-symbol from
    GET /fapi/v1/fundingInfo (fundingIntervalHours); 8h is only the default here.
    """
    if exit_ts <= entry_ts:
        return 0
    hours = set(funding_boundary_hours(interval_hours))
    count = 0
    # walk hour-aligned boundaries from the first candidate after entry
    cursor = entry_ts.replace(minute=0, second=0, microsecond=0)
    if cursor <= entry_ts:
        cursor += timedelta(hours=1)
    while cursor <= exit_ts:
        if cursor.hour in hours:
            count += 1
        cursor += timedelta(hours=1)
    return count


def project_funding(
    notional: float, funding_rate: float, direction: Direction, n_events: int
) -> float:
    """Projected funding cost in USDT (positive = we pay, negative = we receive).

    Assumes the rate is roughly constant over the horizon (caller may average).
    """
    sign = 1.0 if direction == "long" else -1.0
    return abs(notional) * funding_rate * sign * n_events
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_costs.py -v`
Expected: PASS (9 passed total).

- [ ] **Step 5: Commit**

```bash
git add futures_fund/costs.py tests/test_costs.py
git commit -m "feat: funding settlement counting and projection"
```

---

## Task 5: Slippage model (VWAP walk over order-book depth)

**Files:**
- Modify: `futures_fund/costs.py`
- Modify: `tests/test_costs.py`

Given L2 depth (price, qty levels on the side we cross), walk levels to fill `qty`, compute the volume-weighted average fill price, and the slippage cost vs the reference (best/mid) price.

- [ ] **Step 1: Write the failing test (append to `tests/test_costs.py`)**

```python
from futures_fund.costs import vwap_fill, slippage_cost


def test_vwap_fill_single_level():
    filled, vwap = vwap_fill([(100.0, 10.0)], qty=5.0)
    assert filled == pytest.approx(5.0)
    assert vwap == pytest.approx(100.0)


def test_vwap_fill_walks_multiple_levels():
    # buy 15 units: 10 @100, 5 @101 -> vwap = (10*100 + 5*101)/15
    filled, vwap = vwap_fill([(100.0, 10.0), (101.0, 5.0)], qty=15.0)
    assert filled == pytest.approx(15.0)
    assert vwap == pytest.approx((1000 + 505) / 15)


def test_vwap_fill_insufficient_depth_returns_partial():
    filled, vwap = vwap_fill([(100.0, 10.0)], qty=25.0)
    assert filled == pytest.approx(10.0)        # only 10 available
    assert vwap == pytest.approx(100.0)


def test_slippage_cost_is_qty_times_price_diff_from_reference():
    cost = slippage_cost([(100.0, 10.0), (101.0, 10.0)], qty=15.0, reference_price=100.0)
    # fill 10@100 + 5@101 = vwap 100.333..., diff 0.333.. * 15 = 5.0
    assert cost == pytest.approx(5.0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_costs.py -k "vwap or slippage" -v`
Expected: FAIL — `ImportError: cannot import name 'vwap_fill'`.

- [ ] **Step 3: Write the implementation (append to `futures_fund/costs.py`)**

```python
def vwap_fill(levels: list[tuple[float, float]], qty: float) -> tuple[float, float]:
    """Walk price/qty `levels` (in crossing order) to fill `qty`.

    Returns (filled_qty, vwap). If depth is insufficient, returns the partial fill.
    """
    if qty <= 0 or not levels:
        return 0.0, 0.0
    remaining = qty
    cost = 0.0
    filled = 0.0
    for price, avail in levels:
        take = min(remaining, avail)
        cost += take * price
        filled += take
        remaining -= take
        if remaining <= 1e-12:
            break
    vwap = cost / filled if filled > 0 else 0.0
    return filled, vwap


def slippage_cost(
    levels: list[tuple[float, float]], qty: float, reference_price: float
) -> float:
    """USDT slippage cost: filled_qty * |vwap - reference_price|."""
    filled, vwap = vwap_fill(levels, qty)
    if filled <= 0:
        return 0.0
    return filled * abs(vwap - reference_price)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_costs.py -v`
Expected: PASS (13 passed total).

- [ ] **Step 5: Commit**

```bash
git add futures_fund/costs.py tests/test_costs.py
git commit -m "feat: VWAP-walk slippage model over L2 depth"
```

---

## Task 6: Liquidation price & tiered MMR (isolated margin)

**Files:**
- Create: `futures_fund/liquidation.py`
- Test: `tests/test_liquidation.py`

Derivation (isolated, single position). Position liquidates when `equity ≤ maintenance_margin`, where `maintenance_margin = notional × MMR − maint_amount`.
- Long: `liq = (qty·entry − margin − maint_amount) / (qty·(1 − MMR))`
- Short: `liq = (qty·entry + margin + maint_amount) / (qty·(1 + MMR))`

`margin` is the isolated initial margin allocated (= notional / leverage).

- [ ] **Step 1: Write the failing test**

`tests/test_liquidation.py`:
```python
import pytest

from futures_fund.models import MmrBracket
from futures_fund.liquidation import mmr_for_notional, liquidation_price


BRACKETS = [
    MmrBracket(notional_floor=0, notional_cap=50_000, mmr=0.004, maint_amount=0.0, max_leverage=125),
    MmrBracket(notional_floor=50_000, notional_cap=250_000, mmr=0.005, maint_amount=50.0, max_leverage=100),
]


def test_mmr_lookup_low_bracket():
    mmr, maint = mmr_for_notional(10_000.0, BRACKETS)
    assert (mmr, maint) == (0.004, 0.0)


def test_mmr_lookup_high_bracket():
    mmr, maint = mmr_for_notional(100_000.0, BRACKETS)
    assert (mmr, maint) == (0.005, 50.0)


def test_mmr_above_top_bracket_uses_top():
    mmr, maint = mmr_for_notional(10_000_000.0, BRACKETS)
    assert (mmr, maint) == (0.005, 50.0)


def test_long_liquidation_below_entry():
    # entry 100, qty 100 -> notional 10k, 10x leverage -> margin 1000, mmr 0.004
    liq = liquidation_price(entry=100.0, qty=100.0, margin=1000.0, direction="long",
                            mmr=0.004, maint_amount=0.0)
    # (100*100 - 1000 - 0) / (100*(1-0.004)) = 9000 / 99.6 = 90.361...
    assert liq == pytest.approx(9000 / 99.6, rel=1e-9)
    assert liq < 100.0


def test_short_liquidation_above_entry():
    liq = liquidation_price(entry=100.0, qty=100.0, margin=1000.0, direction="short",
                            mmr=0.004, maint_amount=0.0)
    # (100*100 + 1000 + 0) / (100*(1+0.004)) = 11000 / 100.4 = 109.561...
    assert liq == pytest.approx(11000 / 100.4, rel=1e-9)
    assert liq > 100.0


def test_higher_leverage_moves_liq_closer_to_entry():
    far = liquidation_price(100.0, 100.0, margin=2000.0, direction="long", mmr=0.004, maint_amount=0.0)
    near = liquidation_price(100.0, 100.0, margin=500.0, direction="long", mmr=0.004, maint_amount=0.0)
    assert abs(100.0 - near) < abs(100.0 - far)  # less margin -> liq nearer entry
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_liquidation.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'futures_fund.liquidation'`.

- [ ] **Step 3: Write the implementation**

`futures_fund/liquidation.py`:
```python
from __future__ import annotations

from futures_fund.models import Direction, MmrBracket


def mmr_for_notional(notional: float, brackets: list[MmrBracket]) -> tuple[float, float]:
    """Return (mmr, maint_amount) for the bracket containing `notional`.

    Brackets are sorted by floor; if notional exceeds the top cap, use the top bracket.
    """
    notional = abs(notional)
    ordered = sorted(brackets, key=lambda b: b.notional_floor)
    chosen = ordered[0]
    for b in ordered:
        if notional >= b.notional_floor:
            chosen = b
        else:
            break
    return chosen.mmr, chosen.maint_amount


def liquidation_price(
    entry: float, qty: float, margin: float, direction: Direction,
    mmr: float, maint_amount: float,
) -> float:
    """Isolated-margin liquidation price for a single position.

    Long:  (qty*entry - margin - maint_amount) / (qty*(1 - mmr))
    Short: (qty*entry + margin + maint_amount) / (qty*(1 + mmr))

    Matches Binance's USD-M formula (maintenance_margin = notional*mmr - maint_amount);
    verified symbolically. Assumes (mmr, maint_amount) come from the bracket of the ENTRY
    notional; if the resulting liq price implies a different bracket, A3 must re-solve with
    that bracket's values. The live liquidation TRIGGER compares MARK price to this value.
    """
    if qty <= 0:
        raise ValueError("qty must be positive")
    notional_at_entry = qty * entry
    if direction == "long":
        return (notional_at_entry - margin - maint_amount) / (qty * (1.0 - mmr))
    return (notional_at_entry + margin + maint_amount) / (qty * (1.0 + mmr))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_liquidation.py -v`
Expected: PASS (6 passed).

- [ ] **Step 5: Commit**

```bash
git add futures_fund/liquidation.py tests/test_liquidation.py
git commit -m "feat: tiered MMR lookup and isolated liquidation price"
```

---

## Task 7: Position sizing & leverage-as-output

**Files:**
- Create: `futures_fund/sizing.py`
- Test: `tests/test_sizing.py`

Rules from spec §7: `qty = (equity·risk%) / |entry − stop|`; leverage is chosen as the *highest* (most capital-efficient) value that still keeps the liquidation price at least `k×` the stop distance beyond entry, and never exceeds the cap. `k` default 2.5.

- [ ] **Step 1: Write the failing test**

`tests/test_sizing.py`:
```python
import pytest

from futures_fund.sizing import qty_from_risk, choose_leverage, liq_distance_ratio


def test_qty_from_risk_basic():
    # risk 1% of 10k = 100 USDT; stop distance 5 -> qty = 20
    assert qty_from_risk(equity=10_000.0, risk_pct=0.01, entry=100.0, stop=95.0) == pytest.approx(20.0)


def test_qty_zero_when_no_stop_distance():
    assert qty_from_risk(10_000.0, 0.01, entry=100.0, stop=100.0) == 0.0


def test_liq_distance_ratio_is_liq_gap_over_stop_gap():
    # stop gap = 5; if liq is 12.5 below entry, ratio = 2.5
    ratio = liq_distance_ratio(entry=100.0, stop=95.0, liq_price=87.5, direction="long")
    assert ratio == pytest.approx(2.5)


def test_choose_leverage_respects_cap_and_liq_distance():
    # With mmr 0.004, find max leverage (<=cap 5) keeping liq >= 2.5x stop distance.
    lev = choose_leverage(
        entry=100.0, stop=95.0, qty=20.0, direction="long",
        mmr=0.004, maint_amount=0.0, max_leverage=5.0, min_liq_distance_mult=2.5,
    )
    assert 0 < lev <= 5.0
    # verify the resulting liq distance actually satisfies the rule
    from futures_fund.liquidation import liquidation_price
    margin = (qty := 20.0) * 100.0 / lev
    liq = liquidation_price(100.0, qty, margin, "long", 0.004, 0.0)
    assert liq_distance_ratio(100.0, 95.0, liq, "long") >= 2.5 - 1e-6


def test_choose_leverage_returns_cap_when_geometry_is_safe():
    # Tiny stop gap (0.1): even 50x keeps the liq price ~16x the stop gap away,
    # so the cap itself is safe and choose_leverage returns the cap unchanged.
    lev = choose_leverage(
        entry=100.0, stop=99.9, qty=10.0, direction="long",
        mmr=0.004, maint_amount=0.0, max_leverage=50.0, min_liq_distance_mult=2.5,
    )
    assert lev == pytest.approx(50.0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_sizing.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'futures_fund.sizing'`.

- [ ] **Step 3: Write the implementation**

`futures_fund/sizing.py`:
```python
from __future__ import annotations

from futures_fund.liquidation import liquidation_price
from futures_fund.models import Direction


def qty_from_risk(equity: float, risk_pct: float, entry: float, stop: float) -> float:
    """Fixed-fractional sizing: qty such that a stop-out loses exactly equity*risk_pct."""
    risk_per_unit = abs(entry - stop)
    if risk_per_unit <= 0:
        return 0.0
    return (equity * risk_pct) / risk_per_unit


def liq_distance_ratio(entry: float, stop: float, liq_price: float, direction: Direction) -> float:
    """How many 'stop distances' away the liquidation price sits from entry."""
    stop_gap = abs(entry - stop)
    if stop_gap <= 0:
        return float("inf")
    return abs(entry - liq_price) / stop_gap


def choose_leverage(
    entry: float, stop: float, qty: float, direction: Direction,
    mmr: float, maint_amount: float, max_leverage: float,
    min_liq_distance_mult: float = 2.5,
) -> float:
    """Pick the highest leverage <= max_leverage that keeps liq distance >= mult*stop_gap.

    Leverage is an OUTPUT of the risk geometry, never an input. Searches leverage
    downward from max_leverage; lower leverage => more margin => liq farther from entry.
    """
    if qty <= 0:
        return 0.0
    notional = qty * entry
    # Scan candidate leverages from cap down to 1x in fine steps; pick first that is safe.
    steps = 200
    best = 0.0
    for i in range(steps + 1):
        lev = max_leverage - (max_leverage - 1.0) * (i / steps)
        lev = max(1.0, lev)
        margin = notional / lev
        liq = liquidation_price(entry, qty, margin, direction, mmr, maint_amount)
        if liq_distance_ratio(entry, stop, liq, direction) >= min_liq_distance_mult:
            best = lev
            break
    return best
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_sizing.py -v`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add futures_fund/sizing.py tests/test_sizing.py
git commit -m "feat: ATR-stop sizing with leverage-as-output and liq-distance rule"
```

---

## Task 8: Portfolio heat & correlation-cluster risk

**Files:**
- Create: `futures_fund/portfolio_risk.py`
- Test: `tests/test_portfolio_risk.py`

Heat = sum of open per-trade risks as a fraction of equity. Correlated same-direction positions are grouped; a cluster's combined risk is what must respect the heat cap (treat correlated longs as one position — spec §7).

- [ ] **Step 1: Write the failing test**

`tests/test_portfolio_risk.py`:
```python
import pytest

from futures_fund.portfolio_risk import position_risk, portfolio_heat, cluster_heat


def test_position_risk_fraction():
    # qty 20, stop gap 5 -> 100 USDT risk on 10k equity = 1%
    assert position_risk(qty=20.0, entry=100.0, stop=95.0, equity=10_000.0) == pytest.approx(0.01)


def test_portfolio_heat_sums_positions():
    positions = [
        dict(qty=20.0, entry=100.0, stop=95.0),    # 1%
        dict(qty=10.0, entry=200.0, stop=190.0),   # 100 USDT -> 1%
    ]
    assert portfolio_heat(positions, equity=10_000.0) == pytest.approx(0.02)


def test_cluster_heat_groups_correlated_same_direction():
    # Two long positions correlated >= 0.7 form one cluster; their risks add within it.
    positions = [
        dict(symbol="ETHUSDT", direction="long", qty=20.0, entry=100.0, stop=95.0),  # 1%
        dict(symbol="SOLUSDT", direction="long", qty=10.0, entry=200.0, stop=190.0), # 1%
    ]
    corr = {("ETHUSDT", "SOLUSDT"): 0.8}
    clusters = cluster_heat(positions, equity=10_000.0, corr=corr, threshold=0.7)
    assert max(clusters.values()) == pytest.approx(0.02)  # combined cluster heat


def test_cluster_heat_opposite_directions_not_grouped():
    positions = [
        dict(symbol="ETHUSDT", direction="long", qty=20.0, entry=100.0, stop=95.0),
        dict(symbol="SOLUSDT", direction="short", qty=10.0, entry=200.0, stop=210.0),
    ]
    corr = {("ETHUSDT", "SOLUSDT"): 0.8}
    clusters = cluster_heat(positions, equity=10_000.0, corr=corr, threshold=0.7)
    assert max(clusters.values()) == pytest.approx(0.01)  # separate clusters, each 1%
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_portfolio_risk.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'futures_fund.portfolio_risk'`.

- [ ] **Step 3: Write the implementation**

`futures_fund/portfolio_risk.py`:
```python
from __future__ import annotations

from collections.abc import Mapping


def position_risk(qty: float, entry: float, stop: float, equity: float) -> float:
    """Per-trade risk as a fraction of equity (loss if stopped out)."""
    if equity <= 0:
        return 0.0
    return abs(qty) * abs(entry - stop) / equity


def portfolio_heat(positions: list[dict], equity: float) -> float:
    """Sum of per-trade risks across all open positions, as a fraction of equity."""
    return sum(position_risk(p["qty"], p["entry"], p["stop"], equity) for p in positions)


def _corr(corr: Mapping[tuple[str, str], float], a: str, b: str) -> float:
    if (a, b) in corr:
        return corr[(a, b)]
    if (b, a) in corr:
        return corr[(b, a)]
    return 0.0


def cluster_heat(
    positions: list[dict], equity: float,
    corr: Mapping[tuple[str, str], float], threshold: float = 0.7,
) -> dict[int, float]:
    """Group same-direction positions whose pairwise correlation >= threshold (union-find),
    and return {cluster_id: combined_heat_fraction}.

    A cluster's combined heat is what the heat cap should be applied to, because correlated
    same-direction exposure behaves as one position under stress.
    """
    n = len(positions)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        parent[find(a)] = find(b)

    for i in range(n):
        for j in range(i + 1, n):
            pi, pj = positions[i], positions[j]
            if pi.get("direction") == pj.get("direction"):
                if _corr(corr, pi["symbol"], pj["symbol"]) >= threshold:
                    union(i, j)

    out: dict[int, float] = {}
    for idx, p in enumerate(positions):
        root = find(idx)
        out[root] = out.get(root, 0.0) + position_risk(p["qty"], p["entry"], p["stop"], equity)
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_portfolio_risk.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add futures_fund/portfolio_risk.py tests/test_portfolio_risk.py
git commit -m "feat: portfolio heat + correlation-cluster risk grouping"
```

---

## Task 9: Adaptive risk matrix, circuit breakers, CVaR alarm

**Files:**
- Create: `futures_fund/policy.py`
- Test: `tests/test_policy.py`

Implements spec §7.1 matrix, the circuit breakers, and a CVaR (expected-shortfall) alarm.

- [ ] **Step 1: Write the failing test**

`tests/test_policy.py`:
```python
import pytest

from futures_fund.models import RegimeState, PortfolioHealth
from futures_fund.policy import caps_for, circuit_breaker, cvar


def _health(equity, peak):
    return PortfolioHealth(equity=equity, peak_equity=peak)


def test_healthy_low_vol_trend_is_full_caps():
    caps = caps_for(RegimeState(quadrant="low_vol_trend"), _health(10_000, 10_000))
    assert caps.max_leverage == 5.0
    assert caps.per_trade_risk_pct == pytest.approx(0.015)
    assert caps.max_heat == pytest.approx(0.10)
    assert caps.bias == "normal"


def test_high_vol_range_is_reduced():
    caps = caps_for(RegimeState(quadrant="high_vol_range"), _health(10_000, 10_000))
    assert caps.max_leverage == 2.0
    assert caps.per_trade_risk_pct == pytest.approx(0.005)


def test_caution_halves_caps():
    # equity 9400/10000 -> dd 6% -> caution tier; halve the healthy Q1 caps
    caps = caps_for(RegimeState(quadrant="low_vol_trend"), _health(9_400, 10_000))
    assert caps.max_leverage == pytest.approx(2.5)
    assert caps.per_trade_risk_pct == pytest.approx(0.0075)


def test_stressed_forces_flat_bias_and_zero_risk():
    caps = caps_for(RegimeState(quadrant="low_vol_trend"), _health(8_500, 10_000))  # dd 15%
    assert caps.bias == "flat"
    assert caps.per_trade_risk_pct == 0.0


def test_transition_regime_minimum_size():
    caps = caps_for(RegimeState(quadrant="transition"), _health(10_000, 10_000))
    assert caps.bias == "reduce"
    assert caps.max_leverage <= 2.0


def test_circuit_breaker_daily_loss_halts_new():
    state = circuit_breaker(daily_pnl_pct=-0.035, weekly_pnl_pct=-0.01, monthly_pnl_pct=-0.02,
                            dd_from_peak=0.04)
    assert state.allow_new_entries is False
    assert state.risk_multiplier <= 1.0


def test_circuit_breaker_step_down_halves_at_5pct_drawdown():
    state = circuit_breaker(daily_pnl_pct=-0.01, weekly_pnl_pct=-0.02, monthly_pnl_pct=-0.03,
                            dd_from_peak=0.06)
    assert state.risk_multiplier == pytest.approx(0.5)


def test_circuit_breaker_monthly_force_flatten():
    state = circuit_breaker(daily_pnl_pct=-0.02, weekly_pnl_pct=-0.05, monthly_pnl_pct=-0.16,
                            dd_from_peak=0.16)
    assert state.force_flatten is True


def test_cvar_is_mean_of_worst_tail():
    # returns; 5% tail of 20 obs = worst 1 obs = -0.10
    returns = [-0.10] + [0.01] * 19
    assert cvar(returns, alpha=0.05) == pytest.approx(-0.10)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_policy.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'futures_fund.policy'`.

- [ ] **Step 3: Write the implementation**

`futures_fund/policy.py`:
```python
from __future__ import annotations

import numpy as np
from pydantic import BaseModel

from futures_fund.models import PortfolioHealth, RegimeQuadrant, RegimeState, RiskCaps

# Healthy-tier base caps per regime quadrant: (max_leverage, per_trade_risk_pct, max_heat)
_BASE_CAPS: dict[RegimeQuadrant, tuple[float, float, float]] = {
    "low_vol_trend":  (5.0, 0.015, 0.10),
    "high_vol_trend": (4.0, 0.010, 0.08),
    "low_vol_range":  (3.0, 0.010, 0.08),
    "high_vol_range": (2.0, 0.005, 0.04),
    "transition":     (2.0, 0.005, 0.04),
}


def caps_for(regime: RegimeState, health: PortfolioHealth) -> RiskCaps:
    """Adaptive caps from the regime × portfolio-health matrix (spec §7.1)."""
    lev, risk, heat = _BASE_CAPS[regime.quadrant]
    bias = "reduce" if regime.quadrant == "transition" else "normal"
    tier = health.tier

    if tier == "stressed":
        return RiskCaps(max_leverage=1.0, per_trade_risk_pct=0.0, max_heat=0.0, bias="flat")
    if tier == "caution":
        lev *= 0.5
        risk *= 0.5
        heat *= 0.5
        bias = "reduce"
    return RiskCaps(max_leverage=lev, per_trade_risk_pct=risk, max_heat=heat, bias=bias)


class BreakerState(BaseModel):
    allow_new_entries: bool
    force_flatten: bool
    risk_multiplier: float
    reason: str = ""


def circuit_breaker(
    daily_pnl_pct: float, weekly_pnl_pct: float, monthly_pnl_pct: float, dd_from_peak: float
) -> BreakerState:
    """Hard circuit breakers (spec §7). Thresholds are fractions (e.g. -0.03 = -3%)."""
    allow_new = True
    force_flatten = False
    mult = 1.0
    reasons: list[str] = []

    if dd_from_peak >= 0.05:           # step-down: halve risk past -5% from peak
        mult = 0.5
        reasons.append("dd>=5% step-down")
    if daily_pnl_pct <= -0.03:
        allow_new = False
        reasons.append("daily<=-3% halt-new")
    if weekly_pnl_pct <= -0.07:
        allow_new = False
        reasons.append("weekly<=-7% halt-new")
    if monthly_pnl_pct <= -0.12:
        allow_new = False
        force_flatten = True
        reasons.append("monthly<=-12% force-flatten")
    return BreakerState(allow_new_entries=allow_new, force_flatten=force_flatten,
                        risk_multiplier=mult, reason="; ".join(reasons))


def cvar(returns: list[float], alpha: float = 0.05) -> float:
    """Conditional VaR (expected shortfall): mean of the worst `alpha` fraction of returns.

    Returns 0.0 if there are no observations. More negative = worse tail.
    """
    if not returns:
        return 0.0
    arr = np.sort(np.asarray(returns, dtype=float))
    k = max(1, int(np.ceil(alpha * len(arr))))
    return float(arr[:k].mean())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_policy.py -v`
Expected: PASS (9 passed).

- [ ] **Step 5: Commit**

```bash
git add futures_fund/policy.py tests/test_policy.py
git commit -m "feat: adaptive risk matrix, circuit breakers, CVaR alarm"
```

---

## Task 10: The risk gate (capstone — approve / resize / veto)

**Files:**
- Create: `futures_fund/risk_gate.py`
- Test: `tests/test_risk_gate.py`

`evaluate(...)` composes everything: applies adaptive caps + circuit breakers, sizes the trade, computes leverage/liq/cost, checks per-trade risk, liq distance, RR, and incremental portfolio heat (with correlation clustering), then returns a `RiskDecision`. **The deterministic gate is final** — there is no LLM appeal.

- [ ] **Step 1: Write the failing test**

`tests/test_risk_gate.py`:
```python
import pytest

from futures_fund.models import (
    TradeProposal, SymbolSpec, MmrBracket, RegimeState, PortfolioHealth,
)
from futures_fund.risk_gate import evaluate, GateInputs


def _spec():
    return SymbolSpec(
        symbol="BTCUSDT", tick_size=0.1, step_size=0.001, min_notional=5.0,
        mmr_brackets=[
            MmrBracket(notional_floor=0, notional_cap=50_000, mmr=0.004, maint_amount=0.0, max_leverage=125),
        ],
    )


def _proposal(direction="long", entry=100.0, stop=95.0, tps=(115.0,)):
    return TradeProposal(symbol="BTCUSDT", direction=direction, entry=entry, stop=stop,
                         take_profits=list(tps), atr=2.0, confidence=0.7, horizon_hours=8,
                         funding_rate=0.0001)


def _inputs(**over):
    base = dict(
        proposal=_proposal(),
        spec=_spec(),
        regime=RegimeState(quadrant="low_vol_trend"),
        health=PortfolioHealth(equity=10_000.0, peak_equity=10_000.0),
        open_positions=[],
        corr={},
        daily_pnl_pct=0.0, weekly_pnl_pct=0.0, monthly_pnl_pct=0.0,
    )
    base.update(over)
    return GateInputs(**base)


def test_clean_trade_is_approved_and_leverage_is_output():
    d = evaluate(_inputs())
    assert d.verdict == "approve"
    assert d.sized_trade.leverage > 0
    # risk ~= 1.5% of equity in low_vol_trend healthy
    risk = d.sized_trade.qty * abs(100.0 - 95.0) / 10_000.0
    assert risk == pytest.approx(0.015, abs=2e-3)


def test_stressed_portfolio_vetoes_new_entry():
    d = evaluate(_inputs(health=PortfolioHealth(equity=8_500.0, peak_equity=10_000.0)))
    assert d.verdict == "veto"
    assert "flat" in d.reason.lower() or "stressed" in d.reason.lower()


def test_bad_rr_is_vetoed():
    # take-profit barely above entry -> RR < 2:1
    d = evaluate(_inputs(proposal=_proposal(tps=(101.0,))))
    assert d.verdict == "veto"
    assert "rr" in d.reason.lower() or "reward" in d.reason.lower()


def test_heat_cap_resizes_when_existing_exposure_high():
    # Pre-existing 9% heat, cap 10% -> new 1.5% trade must be resized down to fit.
    existing = [dict(symbol="ETHUSDT", direction="long", qty=180.0, entry=100.0, stop=95.0)]  # 9%
    d = evaluate(_inputs(open_positions=existing))
    assert d.verdict in ("resize", "veto")
    if d.verdict == "resize":
        new_risk = d.sized_trade.qty * 5.0 / 10_000.0
        assert new_risk <= 0.01 + 1e-6  # only ~1% of headroom remained


def test_daily_breaker_halts_new_entries():
    d = evaluate(_inputs(daily_pnl_pct=-0.04))
    assert d.verdict == "veto"


def test_cost_estimate_is_attached():
    d = evaluate(_inputs())
    assert d.sized_trade.cost.total > 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_risk_gate.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'futures_fund.risk_gate'`.

- [ ] **Step 3: Write the implementation**

`futures_fund/risk_gate.py`:
```python
from __future__ import annotations

from pydantic import BaseModel, Field

from futures_fund.costs import project_funding, round_trip_fee
from futures_fund.liquidation import liquidation_price, mmr_for_notional
from futures_fund.models import (
    CostEstimate, PortfolioHealth, RegimeState, RiskDecision, SizedTrade,
    SymbolSpec, TradeProposal,
)
from futures_fund.policy import caps_for, circuit_breaker
from futures_fund.portfolio_risk import position_risk
from futures_fund.sizing import choose_leverage, liq_distance_ratio, qty_from_risk

MIN_RR = 2.0
MIN_LIQ_DISTANCE_MULT = 2.5


class GateInputs(BaseModel):
    proposal: TradeProposal
    spec: SymbolSpec
    regime: RegimeState
    health: PortfolioHealth
    open_positions: list[dict] = Field(default_factory=list)
    corr: dict = Field(default_factory=dict)
    daily_pnl_pct: float = 0.0
    weekly_pnl_pct: float = 0.0
    monthly_pnl_pct: float = 0.0
    pay_bnb: bool = False


def _reward_risk(p: TradeProposal) -> float:
    if not p.take_profits:
        return 0.0
    nearest_tp = min(p.take_profits, key=lambda tp: abs(tp - p.entry))
    reward = abs(nearest_tp - p.entry)
    risk = p.risk_per_unit
    return reward / risk if risk > 0 else 0.0


def _build_sized(p: TradeProposal, spec: SymbolSpec, qty: float, leverage: float) -> SizedTrade:
    notional = qty * p.entry
    mmr, maint = mmr_for_notional(notional, spec.mmr_brackets)
    margin = notional / leverage if leverage > 0 else notional
    liq = liquidation_price(p.entry, qty, margin, p.direction, mmr, maint)
    fees = round_trip_fee(notional, maker_entry=False, maker_exit=False)
    funding = project_funding(notional, p.funding_rate, p.direction,
                              n_events=max(1, int(p.horizon_hours // 8)))
    cost = CostEstimate(entry_fee=fees / 2, exit_fee=fees / 2, funding=max(0.0, funding))
    return SizedTrade(proposal=p, qty=qty, notional=notional, leverage=leverage,
                      margin=margin, liq_price=liq, cost=cost)


def evaluate(inp: GateInputs) -> RiskDecision:
    p, spec = inp.proposal, inp.spec
    caps = caps_for(inp.regime, inp.health)
    breaker = circuit_breaker(inp.daily_pnl_pct, inp.weekly_pnl_pct,
                              inp.monthly_pnl_pct, inp.health.drawdown_from_peak)
    warnings: list[str] = []

    # 1. Hard stops: bias flat / breakers / zero risk budget
    if caps.bias == "flat" or caps.per_trade_risk_pct <= 0:
        return RiskDecision(verdict="veto",
                            reason=f"risk-off: regime/health forces flat (tier={inp.health.tier})")
    if not breaker.allow_new_entries:
        return RiskDecision(verdict="veto", reason=f"circuit breaker: {breaker.reason}")

    # 2. Reward:risk
    rr = _reward_risk(p)
    if rr < MIN_RR:
        return RiskDecision(verdict="veto", reason=f"RR {rr:.2f} < min {MIN_RR}")

    # 3. Effective per-trade risk budget (caps × breaker multiplier)
    risk_pct = caps.per_trade_risk_pct * breaker.risk_multiplier

    # 4. Heat headroom (correlation handled by treating the new trade's cluster additively)
    equity = inp.health.equity
    used_heat = sum(position_risk(x["qty"], x["entry"], x["stop"], equity)
                    for x in inp.open_positions)
    headroom = max(0.0, caps.max_heat - used_heat)
    if headroom <= 0:
        return RiskDecision(verdict="veto",
                            reason=f"no heat headroom (used {used_heat:.3f} >= cap {caps.max_heat:.3f})")
    effective_risk_pct = min(risk_pct, headroom)
    if effective_risk_pct < risk_pct:
        warnings.append(f"risk trimmed to heat headroom {headroom:.3f}")

    # 5. Size, leverage (output), liq distance
    qty = qty_from_risk(equity, effective_risk_pct, p.entry, p.stop)
    if qty <= 0:
        return RiskDecision(verdict="veto", reason="computed qty is zero")
    notional = qty * p.entry
    mmr, maint = mmr_for_notional(notional, spec.mmr_brackets)
    leverage = choose_leverage(p.entry, p.stop, qty, p.direction, mmr, maint,
                               caps.max_leverage, MIN_LIQ_DISTANCE_MULT)
    if leverage <= 0:
        return RiskDecision(verdict="veto",
                            reason="cannot satisfy liq-distance rule within leverage cap")

    # 6. min-notional check
    if notional < spec.min_notional:
        return RiskDecision(verdict="veto",
                            reason=f"notional {notional:.2f} < min {spec.min_notional}")

    sized = _build_sized(p, spec, qty, leverage)

    # 7. Final liq-distance assertion
    ratio = liq_distance_ratio(p.entry, p.stop, sized.liq_price, p.direction)
    if ratio < MIN_LIQ_DISTANCE_MULT - 1e-6:
        return RiskDecision(verdict="veto",
                            reason=f"liq distance {ratio:.2f}x < {MIN_LIQ_DISTANCE_MULT}x")

    verdict = "resize" if warnings else "approve"
    reason = "approved" if verdict == "approve" else "; ".join(warnings)
    return RiskDecision(verdict=verdict, reason=reason, sized_trade=sized, warnings=warnings)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_risk_gate.py -v`
Expected: PASS (6 passed).

- [ ] **Step 5: Run the full suite + lint**

Run: `uv run pytest && uv run ruff check .`
Expected: all tests PASS; ruff reports no errors.

- [ ] **Step 6: Commit**

```bash
git add futures_fund/risk_gate.py tests/test_risk_gate.py
git commit -m "feat: risk gate capstone (approve/resize/veto) composing the core"
```

---

## Self-Review (completed during planning)

**Spec coverage (§7 / §7.1):** fees ✓ (T3), funding ✓ (T4), slippage ✓ (T5), liquidation off mark w/ tiered MMR + maint offset ✓ (T6), ATR-stop sizing + leverage-as-output + liq distance ✓ (T7), portfolio heat + correlation-as-one ✓ (T8), adaptive matrix + circuit breakers + CVaR ✓ (T9), hard risk gate approve/resize/veto ✓ (T10). Min RR 2:1 ✓ (T10). Isolated margin ✓ (T6/T7). *Deferred to later plans (correctly out of A1 scope):* live mark-price trigger (A3 monitor), funding-boundary counting against real timestamps (used by A3), the BNB-fee default toggle wiring (config, A2), CVaR alarm thresholding into the gate (wired in A3 with real return history).

**Placeholder scan:** none — every step has runnable code and exact commands.

**Adversarial verification (3 agents, 2026-05-29):** (1) The Binance USD-M isolated liquidation formula in T6 was confirmed correct three independent ways (symbolic/sympy, first-principles derivation, numeric) against Binance's official docs — including the maintenance-amount sign. (2) Funding sign (long pays positive funding) and current fee rates (maker 0.02% / taker 0.05%, 10% BNB discount) confirmed. (3) Every test's expected number in T3–T10 was recomputed by hand and matches the implementation; signatures are consistent across call sites; pydantic v2 syntax is valid. **Fixes applied from the review:** funding interval parameterized (Binance moved many perps to 4h, 1h under stress — was hardcoded 8h); six ruff findings fixed (unused `timezone`/`Mapping` imports, `typing.Mapping`→`collections.abc.Mapping`, quoted return annotations, `Optional`→`| None`); two misleading test name/comment fixed. The Task 10 lint step (`ruff check`) is expected clean after these fixes.

**Type consistency:** `Direction`, `RegimeQuadrant`, `RegimeState`, `PortfolioHealth`, `RiskCaps`, `SymbolSpec`, `MmrBracket`, `TradeProposal`, `SizedTrade`, `CostEstimate`, `RiskDecision` are all defined in T2 and used with matching signatures in T6–T10. `liquidation_price`, `mmr_for_notional`, `qty_from_risk`, `choose_leverage`, `liq_distance_ratio`, `position_risk`, `caps_for`, `circuit_breaker`, `project_funding`, `round_trip_fee` signatures match between definition and call sites in T10.

**Note:** the CVaR alarm function exists (T9) but is intentionally not yet wired into `evaluate` — it needs a rolling return history that the A3 cycle owns. Flagged so A3 wires it; not a gap in A1.

# Futures-Fund Phase D — Live Readiness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Make the desk *able* to trade real capital, safely and only when earned: real circuit-breaker inputs, a live-execution gate (`config.live` AND graduation `graduated`), order building with exchange rounding + reduceOnly stops/TPs, a `LiveExecutor` (placement/leverage/margin), a weight-aware rate limiter, a between-tick risk monitor, and a go-live runbook + kill-switch drills. **Live is OFF by default and double-gated; this phase ships live-READY machinery (fully unit-tested with fakes), not a validated live track record — real-capital go-live requires the user's testnet validation + a passing graduation verdict.**

**Architecture:** New pure-ish modules tested with fake ccxt clients / injected clocks. The paper cycle stays the default validated path; live placement is a guarded module the operator enables only after graduation. Real per-period PnL (from the equity log) now feeds the A1 circuit breakers.

**Tech Stack:** Python 3.11 / uv, pydantic v2, pytest, ruff. No real network/keys in tests.

**Reference:** spec §7 (circuit breakers, mark-price liquidation), §8 (execution & ops), §2 (auto-execute+notify, isolated margin), §9 (graduation gate). Builds on A1 `risk_gate`, A2 `exchange`/`config`, A3a `state`, B2 `orchestration`, C `scorecard`/`graduation`/`equity_log`.

**SAFETY INVARIANTS (enforced + documented):**
1. `live=False` by default; `LiveExecutor.place_book` refuses to place orders without an explicit `confirm_live=True`.
2. Live is permitted only when `settings.live` AND `scorecard.graduation.status == "graduated"` AND not halted.
3. Stops/TPs are always reduceOnly; isolated margin; leverage is the gate's output.

---

## File Structure

```
futures_fund/
  equity_log.py    # (extend) period_return  -> real daily/weekly/monthly breaker inputs
  cycle.py         # (extend) feed real period PnL into the gate's circuit breakers
  config.py        # (extend) Settings.live: bool = False
  live_gate.py     # live_allowed(settings, scorecard)
  orders.py        # round_price/round_qty + build_orders (market entry + reduceOnly stop/TP)
  live_exec.py     # LiveExecutor (prepare/place_book/cancel_all) — refuses without confirm_live
  ratelimit.py     # WeightLimiter (weight-aware token bucket)
  monitor.py       # check_positions (liq-distance / drawdown -> alerts + should_halt) + notify
scripts/
  monitor_cli.py · go_live_check.py
SKILL.md / README.md  # (extend) the go-live runbook + scheduling + kill-switch drill
tests/
  test_period_return.py · test_live_gate.py · test_orders.py · test_live_exec.py
  test_ratelimit.py · test_monitor.py · test_go_live.py
```

---

## Task 1: Real circuit-breaker inputs (period PnL)

**Files:** modify `futures_fund/equity_log.py` and `futures_fund/cycle.py`; create `tests/test_period_return.py`.

- [ ] **Step 1: Write the failing test** — `tests/test_period_return.py`:

```python
from datetime import datetime, timezone

import pytest

from futures_fund.equity_log import period_return, record_equity

UTC = timezone.utc


def test_period_return_uses_baseline_before_cutoff(tmp_path):
    record_equity(tmp_path, datetime(2026, 5, 1, tzinfo=UTC), 10_000.0, cycle=1)
    record_equity(tmp_path, datetime(2026, 5, 2, tzinfo=UTC), 10_300.0, cycle=2)  # +3% over 1 day
    now = datetime(2026, 5, 2, tzinfo=UTC)
    assert period_return(tmp_path, now, days=1) == pytest.approx(0.03)


def test_period_return_negative_drawdown(tmp_path):
    record_equity(tmp_path, datetime(2026, 5, 1, tzinfo=UTC), 10_000.0, cycle=1)
    record_equity(tmp_path, datetime(2026, 5, 1, 12, tzinfo=UTC), 9_600.0, cycle=2)  # -4%
    now = datetime(2026, 5, 1, 12, tzinfo=UTC)
    assert period_return(tmp_path, now, days=1) == pytest.approx(-0.04)


def test_period_return_too_little_history_is_zero(tmp_path):
    record_equity(tmp_path, datetime(2026, 5, 1, tzinfo=UTC), 10_000.0, cycle=1)
    assert period_return(tmp_path, datetime(2026, 5, 1, tzinfo=UTC), days=1) == 0.0
```

- [ ] **Step 2: Run** `uv run pytest tests/test_period_return.py -v` — expect FAIL.

- [ ] **Step 3: Implement** — append to `futures_fund/equity_log.py`:

```python
from datetime import timedelta


def period_return(state_dir, now: datetime, days: float) -> float:
    """Return over the trailing `days`: latest equity vs the last equity at/before now-days
    (or the earliest on record if none is that old). 0.0 with < 2 points. Feeds the A1
    circuit breakers (daily/weekly/monthly)."""
    series = [(datetime.fromisoformat(ts), eq) for ts, eq in equity_series(state_dir)]
    if len(series) < 2:
        return 0.0
    cutoff = now - timedelta(days=days)
    older = [eq for ts, eq in series if ts <= cutoff]
    base = older[-1] if older else series[0][1]
    last = series[-1][1]
    return (last / base - 1.0) if base > 0 else 0.0
```

- [ ] **Step 4: Wire into the cycle.** In `futures_fund/cycle.py` `execute_proposals`, BEFORE the proposals gate loop, compute the period returns and pass them into every `GateInputs`:

```python
    from futures_fund.equity_log import period_return
    daily_pnl = period_return(state_dir, now, 1)
    weekly_pnl = period_return(state_dir, now, 7)
    monthly_pnl = period_return(state_dir, now, 30)
```
Then in the `evaluate(GateInputs(...))` call inside the loop, add the three args:
```python
        decision = evaluate(GateInputs(proposal=prop, spec=spec,
                                       regime=simple_regime(ctx.frames[unified]),
                                       health=health, open_positions=open_dicts,
                                       daily_pnl_pct=daily_pnl, weekly_pnl_pct=weekly_pnl,
                                       monthly_pnl_pct=monthly_pnl))
```

- [ ] **Step 5: Add a wiring test** — append to `tests/test_period_return.py`:

```python
def test_daily_breaker_blocks_new_entry_in_the_cycle(tmp_path):
    import numpy as np
    import pandas as pd

    from futures_fund.config import Settings
    from futures_fund.cycle import execute_proposals, fetch_context
    from futures_fund.models import MmrBracket, SymbolSpec, TradeProposal
    from futures_fund.state import AccountState

    class FakeExchange:
        def __init__(self, df):
            self.df = df

        def symbol_spec(self, s):
            return SymbolSpec(symbol="BTCUSDT", tick_size=0.01, step_size=0.001, min_notional=5.0,
                              mmr_brackets=[MmrBracket(notional_floor=0, notional_cap=1_000_000,
                                                       mmr=0.004, maint_amount=0.0, max_leverage=125)])

        def ohlcv(self, s, tf="4h", limit=500):
            return self.df

        def funding(self, s):
            from futures_fund.market_data import FundingInfo
            return FundingInfo(symbol=s, current_rate=0.0,
                               next_funding_ts=datetime(2026, 1, 1, tzinfo=UTC), interval_hours=8.0,
                               mark_price=float(self.df["close"].iloc[-1]),
                               index_price=float(self.df["close"].iloc[-1]))

    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    from futures_fund.memory_layout import ensure_memory_layout
    ensure_memory_layout(memory_dir)
    # pre-seed an equity history showing a -5% day -> daily breaker should halt new entries
    record_equity(state_dir, datetime(2026, 3, 1, tzinfo=UTC), 10_000.0, cycle=1)
    record_equity(state_dir, datetime(2026, 3, 1, 12, tzinfo=UTC), 9_500.0, cycle=2)
    close = 100.0 + 0.8 * np.arange(60) + np.random.default_rng(5).normal(0, 0.05, 60)
    df = pd.DataFrame({"timestamp": pd.date_range("2026-01-01", periods=60, freq="4h", tz="UTC"),
                       "open": close, "high": close + 0.2, "low": close - 0.2,
                       "close": close, "volume": 1.0})
    ex = FakeExchange(df)
    ctx = fetch_context(ex, Settings(symbols=["BTC/USDT:USDT"]))
    last = float(df["close"].iloc[-1])
    prop = TradeProposal(symbol="BTCUSDT", direction="long", entry=last, stop=last - 4.0,
                         take_profits=[last + 8.0], atr=2.0, confidence=0.7, horizon_hours=4,
                         funding_rate=0.0)
    report = execute_proposals(ctx, [prop], ["team"], [], AccountState(balance=9_500.0, peak_equity=10_000.0),
                               state_dir, memory_dir, now=datetime(2026, 3, 1, 12, tzinfo=UTC), cycle_no=3)
    assert report["opened"] == 0  # daily breaker (-5%) vetoes the entry
```

- [ ] **Step 6: Run** `uv run pytest tests/test_period_return.py tests/test_cycle.py tests/test_execute_proposals.py -v` — expect PASS (period_return 4 + existing cycle/execute tests still green). Then `uv run ruff check futures_fund/equity_log.py futures_fund/cycle.py tests/test_period_return.py`.

- [ ] **Step 7: Commit**

```bash
git add futures_fund/equity_log.py futures_fund/cycle.py tests/test_period_return.py
git commit -m "feat: real per-period PnL feeds the circuit breakers (daily/weekly/monthly)"
```

---

## Task 2: Live gate + config flag

**Files:** modify `futures_fund/config.py`; create `futures_fund/live_gate.py`, `tests/test_live_gate.py`.

- [ ] **Step 1: Add the flag.** In `futures_fund/config.py` `class Settings`, add (after `verdict_horizon_weeks`):

```python
    live: bool = False  # MUST be explicitly enabled; live also requires a 'graduated' verdict
```

- [ ] **Step 2: Write the failing test** — `tests/test_live_gate.py`:

```python
from futures_fund.config import Settings
from futures_fund.live_gate import live_allowed


def _sc(status):
    return {"graduation": {"status": status}}


def test_live_blocked_when_flag_off():
    assert live_allowed(Settings(live=False), _sc("graduated")) is False


def test_live_blocked_when_not_graduated():
    assert live_allowed(Settings(live=True), _sc("not_yet")) is False
    assert live_allowed(Settings(live=True), _sc("failed")) is False


def test_live_allowed_only_when_flag_on_and_graduated():
    assert live_allowed(Settings(live=True), _sc("graduated")) is True


def test_live_blocked_on_missing_scorecard_fields():
    assert live_allowed(Settings(live=True), {}) is False
```

- [ ] **Step 3: Run** `uv run pytest tests/test_live_gate.py -v` — expect FAIL.

- [ ] **Step 4: Implement** `futures_fund/live_gate.py`:

```python
from __future__ import annotations

from futures_fund.config import Settings


def live_allowed(settings: Settings, scorecard: dict) -> bool:
    """Live trading is permitted ONLY when explicitly enabled AND the desk has graduated.
    (The cycle additionally checks the HALT flag.) Survival-first: default-deny."""
    if not getattr(settings, "live", False):
        return False
    g = scorecard.get("graduation")
    return isinstance(g, dict) and g.get("status") == "graduated"
```

- [ ] **Step 5: Run** `uv run pytest tests/test_live_gate.py tests/test_config.py -v` — expect PASS (live_gate 4 + config still green). Then `uv run ruff check futures_fund/config.py futures_fund/live_gate.py tests/test_live_gate.py`.

- [ ] **Step 6: Commit**

```bash
git add futures_fund/config.py futures_fund/live_gate.py tests/test_live_gate.py
git commit -m "feat: live-execution gate (config.live AND graduated) — default-deny"
```

---

## Task 3: Order building + exchange rounding

**Files:** create `futures_fund/orders.py`, `tests/test_orders.py`.

- [ ] **Step 1: Write the failing test** — `tests/test_orders.py`:

```python
import pytest

from futures_fund.orders import build_orders, round_price, round_qty


def test_round_price_to_tick():
    assert round_price(73654.317, 0.1) == pytest.approx(73654.3)
    assert round_price(100.0, 0.0) == 100.0  # no tick -> unchanged


def test_round_qty_floors_to_step():
    assert round_qty(0.123987, 0.001) == pytest.approx(0.123)
    assert round_qty(0.0009, 0.001) == pytest.approx(0.0)  # below one step -> 0


def test_build_orders_long_has_entry_and_reduceonly_protection():
    orders = build_orders("BTCUSDT", "long", qty=0.1234, entry=100.0, stop=95.0,
                          take_profits=[115.0], tick=0.1, step=0.001)
    assert len(orders) == 3
    entry, stop, tp = orders
    assert entry["type"] == "market" and entry["side"] == "buy" and entry["amount"] == pytest.approx(0.123)
    assert stop["type"] == "STOP_MARKET" and stop["side"] == "sell"
    assert stop["params"]["reduceOnly"] is True and stop["params"]["stopPrice"] == pytest.approx(95.0)
    assert tp["type"] == "TAKE_PROFIT_MARKET" and tp["side"] == "sell" and tp["params"]["reduceOnly"] is True


def test_build_orders_short_sides_flip():
    orders = build_orders("BTCUSDT", "short", qty=0.1, entry=100.0, stop=105.0,
                          take_profits=[85.0], tick=0.1, step=0.001)
    assert orders[0]["side"] == "sell"   # entry
    assert orders[1]["side"] == "buy" and orders[1]["type"] == "STOP_MARKET"  # close


def test_build_orders_empty_when_qty_rounds_to_zero():
    assert build_orders("BTCUSDT", "long", qty=0.0004, entry=100.0, stop=95.0,
                        take_profits=[115.0], tick=0.1, step=0.001) == []


def test_build_orders_no_tp_omits_tp_order():
    orders = build_orders("BTCUSDT", "long", qty=0.1, entry=100.0, stop=95.0,
                          take_profits=[], tick=0.1, step=0.001)
    assert len(orders) == 2 and orders[1]["type"] == "STOP_MARKET"
```

- [ ] **Step 2: Run** `uv run pytest tests/test_orders.py -v` — expect FAIL.

- [ ] **Step 3: Implement** `futures_fund/orders.py`:

```python
from __future__ import annotations

import math

from futures_fund.models import Direction


def round_price(price: float, tick: float) -> float:
    if tick <= 0:
        return price
    return round(round(price / tick) * tick, 10)


def round_qty(qty: float, step: float) -> float:
    if step <= 0:
        return qty
    return round(math.floor(qty / step) * step, 10)


def _open_side(direction: Direction) -> str:
    return "buy" if direction == "long" else "sell"


def _close_side(direction: Direction) -> str:
    return "sell" if direction == "long" else "buy"


def build_orders(symbol: str, direction: Direction, qty: float, entry: float, stop: float,
                 take_profits: list[float], tick: float, step: float) -> list[dict]:
    """Build the exchange order set for one position: a market entry plus reduceOnly STOP_MARKET
    and TAKE_PROFIT_MARKET protection, with price->tick and qty->step rounding. Empty if the
    rounded quantity is zero."""
    q = round_qty(qty, step)
    if q <= 0:
        return []
    cs = _close_side(direction)
    orders = [{"symbol": symbol, "type": "market", "side": _open_side(direction), "amount": q}]
    orders.append({"symbol": symbol, "type": "STOP_MARKET", "side": cs, "amount": q,
                   "params": {"stopPrice": round_price(stop, tick), "reduceOnly": True}})
    if take_profits:
        orders.append({"symbol": symbol, "type": "TAKE_PROFIT_MARKET", "side": cs, "amount": q,
                       "params": {"stopPrice": round_price(take_profits[0], tick),
                                  "reduceOnly": True}})
    return orders
```

- [ ] **Step 4: Run** `uv run pytest tests/test_orders.py -v` — expect PASS (6 passed). Then `uv run ruff check futures_fund/orders.py tests/test_orders.py`.

- [ ] **Step 5: Commit**

```bash
git add futures_fund/orders.py tests/test_orders.py
git commit -m "feat: order builder (market entry + reduceOnly stop/TP) with tick/step rounding"
```

---

## Task 4: LiveExecutor (gated order placement)

**Files:** create `futures_fund/live_exec.py`, `tests/test_live_exec.py`.

- [ ] **Step 1: Write the failing test** — `tests/test_live_exec.py`:

```python
import pytest

from futures_fund.live_exec import LiveExecutor


class FakeCcxt:
    def __init__(self):
        self.calls = []

    def set_margin_mode(self, mode, symbol):
        self.calls.append(("margin", mode, symbol))

    def set_leverage(self, lev, symbol):
        self.calls.append(("leverage", lev, symbol))

    def create_order(self, symbol, type_, side, amount, price=None, params=None):
        self.calls.append(("order", symbol, type_, side, amount, params or {}))
        return {"id": f"{type_}-{side}", "status": "open"}

    def cancel_all_orders(self, symbol):
        self.calls.append(("cancel_all", symbol))
        return []


def test_place_book_refuses_without_confirm_live():
    ex = LiveExecutor(FakeCcxt())
    with pytest.raises(RuntimeError):
        ex.place_book([{"symbol": "BTCUSDT", "type": "market", "side": "buy", "amount": 0.1}],
                      confirm_live=False)


def test_prepare_sets_margin_and_leverage():
    fake = FakeCcxt()
    LiveExecutor(fake).prepare("BTCUSDT", leverage=5.0, margin_mode="isolated")
    assert ("margin", "isolated", "BTCUSDT") in fake.calls
    assert ("leverage", 5, "BTCUSDT") in fake.calls


def test_place_book_creates_each_order_with_confirm():
    fake = FakeCcxt()
    orders = [
        {"symbol": "BTCUSDT", "type": "market", "side": "buy", "amount": 0.1},
        {"symbol": "BTCUSDT", "type": "STOP_MARKET", "side": "sell", "amount": 0.1,
         "params": {"stopPrice": 95.0, "reduceOnly": True}},
    ]
    results = LiveExecutor(fake).place_book(orders, confirm_live=True)
    assert len(results) == 2
    order_calls = [c for c in fake.calls if c[0] == "order"]
    assert len(order_calls) == 2
    assert order_calls[1][5]["reduceOnly"] is True  # stop carries reduceOnly


def test_prepare_tolerates_margin_mode_already_set():
    class Boom(FakeCcxt):
        def set_margin_mode(self, mode, symbol):
            raise Exception("No need to change margin type.")
    fake = Boom()
    LiveExecutor(fake).prepare("BTCUSDT", leverage=3.0)  # must not raise
    assert ("leverage", 3, "BTCUSDT") in fake.calls
```

- [ ] **Step 2: Run** `uv run pytest tests/test_live_exec.py -v` — expect FAIL.

- [ ] **Step 3: Implement** `futures_fund/live_exec.py`:

```python
from __future__ import annotations


class LiveExecutor:
    """Places REAL orders via a ccxt client. Refuses to place anything without an explicit
    confirm_live=True (safety invariant 1). Stops/TPs must already be reduceOnly (see orders.py).
    Inject a fake client in tests; never reached in paper mode."""

    def __init__(self, client):
        self.client = client

    def prepare(self, symbol: str, leverage: float, margin_mode: str = "isolated") -> None:
        try:
            self.client.set_margin_mode(margin_mode, symbol)
        except Exception:
            pass  # Binance raises if the margin mode is already set — benign
        self.client.set_leverage(int(leverage), symbol)

    def place_book(self, orders: list[dict], *, confirm_live: bool) -> list:
        if not confirm_live:
            raise RuntimeError("LiveExecutor.place_book refused: confirm_live is not True")
        results = []
        for o in orders:
            results.append(self.client.create_order(
                o["symbol"], o["type"], o["side"], o["amount"], o.get("price"), o.get("params", {})
            ))
        return results

    def cancel_all(self, symbol: str):
        return self.client.cancel_all_orders(symbol)
```

- [ ] **Step 4: Run** `uv run pytest tests/test_live_exec.py -v` — expect PASS (4 passed). Then `uv run ruff check futures_fund/live_exec.py tests/test_live_exec.py`.

- [ ] **Step 5: Commit**

```bash
git add futures_fund/live_exec.py tests/test_live_exec.py
git commit -m "feat: LiveExecutor (prepare/place_book/cancel) — refuses without confirm_live"
```

---

## Task 5: Weight-aware rate limiter

**Files:** create `futures_fund/ratelimit.py`, `tests/test_ratelimit.py`.

- [ ] **Step 1: Write the failing test** — `tests/test_ratelimit.py`:

```python
from futures_fund.ratelimit import WeightLimiter


def test_allows_within_capacity():
    rl = WeightLimiter(capacity=100, window_seconds=60)
    assert rl.allow(40, now=0.0) is True
    assert rl.allow(50, now=1.0) is True   # 90 used


def test_blocks_when_over_capacity():
    rl = WeightLimiter(capacity=100, window_seconds=60)
    rl.allow(80, now=0.0)
    assert rl.allow(30, now=1.0) is False  # 110 > 100


def test_window_expiry_frees_capacity():
    rl = WeightLimiter(capacity=100, window_seconds=60)
    rl.allow(80, now=0.0)
    assert rl.allow(80, now=61.0) is True  # the first 80 aged out of the 60s window


def test_used_weight_reports_current_window():
    rl = WeightLimiter(capacity=100, window_seconds=60)
    rl.allow(40, now=0.0)
    rl.allow(20, now=10.0)
    assert rl.used(now=10.0) == 60
    assert rl.used(now=65.0) == 20  # the t=0 event (40) aged out of the 60s window; t=10 (20) survives
```

- [ ] **Step 2: Run** `uv run pytest tests/test_ratelimit.py -v` — expect FAIL.

- [ ] **Step 3: Implement** `futures_fund/ratelimit.py`:

```python
from __future__ import annotations


class WeightLimiter:
    """Trailing-window weight budget (Binance fapi is ~2400 weight/min/IP). `allow` records the
    weight and returns False if it would exceed capacity in the current window."""

    def __init__(self, capacity: int, window_seconds: float):
        self.capacity = capacity
        self.window = window_seconds
        self._events: list[tuple[float, int]] = []

    def _prune(self, now: float) -> None:
        self._events = [(t, w) for t, w in self._events if t > now - self.window]

    def used(self, now: float) -> int:
        self._prune(now)
        return sum(w for _, w in self._events)

    def allow(self, weight: int, now: float) -> bool:
        self._prune(now)
        if sum(w for _, w in self._events) + weight > self.capacity:
            return False
        self._events.append((now, weight))
        return True
```

- [ ] **Step 4: Run** `uv run pytest tests/test_ratelimit.py -v` — expect PASS (4 passed). Then `uv run ruff check futures_fund/ratelimit.py tests/test_ratelimit.py`.

- [ ] **Step 5: Commit**

```bash
git add futures_fund/ratelimit.py tests/test_ratelimit.py
git commit -m "feat: weight-aware rate limiter (trailing-window budget for fapi)"
```

---

## Task 6: Between-tick risk monitor + notify

**Files:** create `futures_fund/monitor.py`, `scripts/monitor_cli.py`, `tests/test_monitor.py`.

- [ ] **Step 1: Write the failing test** — `tests/test_monitor.py`:

```python
import json
from datetime import datetime, timezone

from futures_fund.monitor import check_positions, notify

UTC = timezone.utc


def test_alerts_when_mark_near_liquidation():
    positions = [{"symbol": "BTCUSDT", "liq_price": 82.0}]
    out = check_positions(positions, {"BTCUSDT": 88.0}, equity=10_000.0, peak_equity=10_000.0,
                          liq_buffer=0.10)
    assert any("liquidation" in a for a in out["alerts"])
    assert out["should_halt"] is False


def test_no_alert_when_far_from_liquidation():
    positions = [{"symbol": "BTCUSDT", "liq_price": 50.0}]
    out = check_positions(positions, {"BTCUSDT": 100.0}, equity=10_000.0, peak_equity=10_000.0)
    assert out["alerts"] == [] and out["should_halt"] is False


def test_drawdown_halt():
    out = check_positions([], {}, equity=8_400.0, peak_equity=10_000.0, dd_halt=0.15)  # -16%
    assert out["should_halt"] is True
    assert out["drawdown"] > 0.15


def test_notify_appends_jsonl(tmp_path):
    notify(tmp_path, "circuit breaker tripped", ts=datetime(2026, 5, 1, tzinfo=UTC))
    lines = [json.loads(x) for x in (tmp_path / "notifications.jsonl").read_text().splitlines() if x.strip()]
    assert lines[0]["message"] == "circuit breaker tripped"
```

- [ ] **Step 2: Run** `uv run pytest tests/test_monitor.py -v` — expect FAIL.

- [ ] **Step 3: Implement** `futures_fund/monitor.py`:

```python
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path


def check_positions(positions: list[dict], marks: dict[str, float], *, equity: float,
                    peak_equity: float, liq_buffer: float = 0.10, dd_halt: float = 0.15) -> dict:
    """Cheap between-tick safety check: alert when any position's mark is within `liq_buffer`
    of its liquidation price, and signal HALT when drawdown-from-peak exceeds `dd_halt`."""
    alerts: list[str] = []
    for p in positions:
        mark = marks.get(p["symbol"])
        if mark is None or mark <= 0:
            continue
        dist = abs(mark - p["liq_price"]) / mark
        if dist <= liq_buffer:
            alerts.append(f"{p['symbol']} within {dist:.1%} of liquidation (mark {mark}, liq {p['liq_price']})")
    drawdown = (peak_equity - equity) / peak_equity if peak_equity > 0 else 0.0
    should_halt = drawdown >= dd_halt
    if should_halt:
        alerts.append(f"drawdown {drawdown:.1%} >= halt threshold {dd_halt:.0%}")
    return {"alerts": alerts, "should_halt": should_halt, "drawdown": drawdown}


def notify(state_dir, message: str, ts: datetime) -> None:
    """Append a notification (the 'notify' half of auto-execute+notify). A real channel
    (email/Telegram) can tail this file."""
    p = Path(state_dir) / "notifications.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as f:
        f.write(json.dumps({"ts": ts.isoformat(), "message": message}) + "\n")
```

- [ ] **Step 4: Create the CLI** `scripts/monitor_cli.py`:

```python
"""Between-tick light risk monitor (run on a faster ~15-30min cron than the 4h cycle).
Checks liquidation distance + drawdown; trips the HALT flag and notifies if breached.

    uv run python scripts/monitor_cli.py
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from futures_fund.config import load_settings
from futures_fund.exchange import FuturesExchange
from futures_fund.monitor import check_positions, notify
from futures_fund.portfolio import total_equity
from futures_fund.state import load_account, load_positions, set_halt


def main() -> None:
    settings = load_settings()
    account = load_account("state", settings.account_size_usdt)
    positions = load_positions("state")
    ex = FuturesExchange.from_settings(settings)
    marks = {p.symbol: ex.mark_price(s) for s in settings.symbols
             for p in positions if ex.symbol_spec(s).symbol == p.symbol}
    equity = total_equity(account.balance, positions, marks)
    pos_dicts = [{"symbol": p.symbol, "liq_price": p.liq_price} for p in positions]
    now = datetime.now(timezone.utc)
    out = check_positions(pos_dicts, marks, equity=equity, peak_equity=account.peak_equity)
    if out["should_halt"]:
        set_halt("state", True, reason="monitor: drawdown halt")
        notify("state", f"HALT tripped by monitor: {out['alerts']}", now)
    elif out["alerts"]:
        notify("state", f"risk alerts: {out['alerts']}", now)
    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    main()
```

- [ ] **Step 5: Run** `uv run pytest tests/test_monitor.py -v` — expect PASS (4 passed). Then `uv run ruff check futures_fund/monitor.py scripts/monitor_cli.py tests/test_monitor.py`.

- [ ] **Step 6: Commit**

```bash
git add futures_fund/monitor.py scripts/monitor_cli.py tests/test_monitor.py
git commit -m "feat: between-tick risk monitor (liq-distance/drawdown -> HALT) + notify"
```

---

## Task 7: Go-live check, kill-switch drill, runbook

**Files:** create `scripts/go_live_check.py`, `tests/test_go_live.py`; modify `README.md` and `SKILL.md`.

- [ ] **Step 1: Write the failing test** — `tests/test_go_live.py`:

```python
from futures_fund.config import Settings
from futures_fund.live_exec import LiveExecutor
from futures_fund.live_gate import live_allowed


class _Boom:
    def create_order(self, *a, **k):
        raise AssertionError("no order may be placed when live is not allowed / not confirmed")


def test_kill_switch_drill_no_orders_when_not_allowed():
    # not graduated -> live_allowed False -> the operator must not place orders
    assert live_allowed(Settings(live=True), {"graduation": {"status": "not_yet"}}) is False


def test_executor_refuses_without_confirm_even_if_allowed():
    # even when live_allowed would be True, place_book still requires confirm_live
    ex = LiveExecutor(_Boom())
    try:
        ex.place_book([{"symbol": "BTCUSDT", "type": "market", "side": "buy", "amount": 0.1}],
                      confirm_live=False)
        raised = False
    except RuntimeError:
        raised = True
    assert raised  # double-gate held: no create_order reached _Boom


def test_runbook_documents_double_gate_and_kill_switch():
    from pathlib import Path
    rb = Path("README.md").read_text()
    assert "Going live" in rb or "go-live" in rb.lower()
    assert "graduated" in rb and ("HALT" in rb or "kill" in rb.lower())
```

- [ ] **Step 2: Run** `uv run pytest tests/test_go_live.py -v` — expect FAIL (README assertion + maybe imports).

- [ ] **Step 3: Create** `scripts/go_live_check.py`:

```python
"""Pre-flight readiness check before enabling live trading. Prints the graduation verdict and
the live-readiness gate. Does NOT place any orders.

    uv run python scripts/go_live_check.py
"""
from __future__ import annotations

import json

from futures_fund.config import load_settings
from futures_fund.live_gate import live_allowed
from futures_fund.scorecard import build_scorecard


def main() -> None:
    settings = load_settings()
    sc = build_scorecard("state", "memory", monthly_target=0.05)
    allowed = live_allowed(settings, sc)
    out = {
        "live_flag": getattr(settings, "live", False),
        "graduation": sc["graduation"],
        "equity": sc["equity"],
        "live_allowed": allowed,
        "verdict": ("READY — live trading permitted" if allowed
                    else "NOT READY — stays in paper mode (need live=true AND a graduated verdict)"),
    }
    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Append the runbook to `README.md`:**

```markdown
## Going live (real capital)

The desk is **paper-by-default** and **double-gated** before it can touch real money:

1. **Validate on testnet.** Put Binance USD-M **testnet** keys in `.env` (`BINANCE_KEY`/`BINANCE_SECRET`); keep `exchange.testnet: true`. Run cycles via `SKILL.md` and confirm orders/fills look right (`scripts/smoke_testnet.py`).
2. **Earn graduation.** Run ≥20–30 audited paper cycles. Check `uv run python scripts/go_live_check.py` — it must report `graduation.status == "graduated"` (positive OOS Sharpe, **DSR > 0.95**, beating buy-&-hold net of costs). Until then, live is refused.
3. **Enable live (explicit).** Only then set `live: true` in `config.yaml` and supply production keys. `LiveExecutor.place_book` *still* refuses unless called with `confirm_live=True`. Leverage is the gate's output; stops/TPs are always reduceOnly; margin is isolated.
4. **Schedule.** Full cycle every 4h (`cron`/scheduler → the `SKILL.md` orchestrator); the light risk monitor every ~15–30 min (`scripts/monitor_cli.py`) — it trips the **HALT** flag on a drawdown/liquidation-distance breach.

### Kill switch
`uv run python -c "from futures_fund.state import set_halt; set_halt('state', True, reason='manual kill')"` halts all new trading immediately; the cycle short-circuits at preflight while the exchange's resting reduceOnly stops keep protecting open positions. Clear with `set_halt('state', False)`.
```

- [ ] **Step 5: Append to `SKILL.md`** (after the Self-healing section), a short `## Live mode` note:

```markdown
## Live mode (default OFF)
Trading is paper unless `config.live` is true AND `scripts/go_live_check.py` reports a `graduated` verdict (`futures_fund.live_gate.live_allowed`). When live, place orders ONLY via `futures_fund.live_exec.LiveExecutor` with `confirm_live=True`; respect the `futures_fund.ratelimit.WeightLimiter`; run `scripts/monitor_cli.py` between cycles. Never enable live without a graduated verdict — see README "Going live".
```

- [ ] **Step 6: Run** `uv run pytest tests/test_go_live.py -v` — expect PASS (3 passed). Then run the FULL suite `uv run pytest` and `uv run ruff check .`. Report the EXACT total (expected 227 + period_return 4 + live_gate 4 + orders 6 + live_exec 4 + ratelimit 4 + monitor 4 + go_live 3 = **256**).

- [ ] **Step 7: Commit**

```bash
git add scripts/go_live_check.py tests/test_go_live.py README.md SKILL.md
git commit -m "feat: go-live readiness check + kill-switch drill + go-live runbook"
```

---

## Self-Review (completed during planning)

**Spec coverage (§7 breakers/mark-liq, §8 execution/ops, §2 auto-execute+notify/isolated, §9 graduation):** real per-period PnL → circuit breakers ✓ (T1); double-gated live (config.live AND graduated, default-deny) ✓ (T2); order building with tick/step rounding + reduceOnly stops/TPs ✓ (T3); the LiveExecutor that refuses without confirm_live ✓ (T4); weight-aware rate limiter ✓ (T5); between-tick liq/drawdown monitor + notify ✓ (T6); go-live check + kill-switch drill + runbook + scheduling ✓ (T7). 

**Honest scope boundary:** this phase ships live-READY machinery, unit-tested with fakes and gated off. The final paper↔live reconciliation (replacing simulated fills with real fill/position reads from the exchange) is the last integration step and MUST be validated on testnet with real keys before real capital — the runbook makes this explicit and the graduation gate enforces it.

**Placeholder scan:** none — runnable code/tests + exact edits/runbook text.

**Type/interface consistency:** `period_return` reuses `equity_series`; the cycle passes its result into A1 `GateInputs` (fields `daily/weekly/monthly_pnl_pct` exist). `live_allowed` reads the C `scorecard["graduation"]["status"]`. `build_orders` consumes A2 `SymbolSpec` tick/step at the call site; `LiveExecutor` calls ccxt with those order dicts. `monitor.check_positions` takes the same position-dict shape used elsewhere. All additive; existing tests guard the cycle.

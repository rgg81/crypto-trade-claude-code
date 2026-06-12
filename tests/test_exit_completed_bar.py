"""Realism fix (found live in cycle 77): the exit pass `audit_and_reflect` must evaluate the latest
COMPLETED 4h candle — NOT the still-forming `iloc[-1]` snapshot taken near the candle's open. With
the old forming-bar read, a stop/TP wick that landed LATER in a candle (after preflight's single
snapshot) fell into the completed `iloc[-2]` row the next cycle and was NEVER checked — so a live
resting stop a real desk would have filled was silently dodged. Exits now mirror the trigger path
(last_completed_frame): every completed candle's full range is checked exactly once."""
from datetime import UTC, datetime

import pandas as pd

from futures_fund.cycle import audit_and_reflect, fetch_context
from futures_fund.state import AccountState, Position
from tests.test_orchestration import FakeExchange, _settings


def _frame_with_lows(completed_low, forming_low, last_open):
    """4 clean candles whose last (forming) row opened at `last_open`; the prior COMPLETED row and
    the forming row carry the supplied lows. Everything else sits at 100/101 (no breach)."""
    ts = pd.date_range(end=last_open, periods=4, freq="4h", tz="UTC")
    lows = [100.0, 100.0, completed_low, forming_low]   # row -2 = completed, row -1 = forming
    return pd.DataFrame({"timestamp": ts, "open": [100.0] * 4, "high": [101.0] * 4,
                         "low": lows, "close": [100.0] * 4, "volume": [1.0] * 4})


def _long_pos():
    return Position(symbol="BTCUSDT", direction="long", qty=1.0, entry=100.0, stop=95.0,
                    take_profits=[110.0], leverage=1.0, margin=100.0, liq_price=50.0,
                    opened_cycle=1, opened_ts=datetime(2026, 6, 12, 0, 0, tzinfo=UTC))


_LAST_OPEN = pd.Timestamp("2026-06-12 08:00", tz="UTC")
_NOW = datetime(2026, 6, 12, 8, 30, tzinfo=UTC)   # inside the 08:00-12:00 forming window


def test_stop_breached_in_completed_candle_is_detected(tmp_path):
    # completed candle low 94 breaches the 95 stop; forming candle low 98 does NOT. The exit pass
    # must read the COMPLETED candle and close the position (the cy77 wick the old code missed).
    df = _frame_with_lows(completed_low=94.0, forming_low=98.0, last_open=_LAST_OPEN)
    ctx = fetch_context(FakeExchange({"BTC/USDT:USDT": df}), _settings())
    report = {"carried": 0, "closed": 0, "actions": []}
    still = audit_and_reflect(ctx, [_long_pos()], AccountState(balance=10_000.0,
                              peak_equity=10_000.0), tmp_path / "m", _NOW, report)
    assert report["closed"] == 1
    assert still == []
    assert report["actions"][0]["reason"] == "stop"


def test_forming_only_wick_is_not_prematurely_fired(tmp_path):
    # the mirror contract: a wick that is ONLY in the still-forming candle (completed row clean) is
    # NOT acted on until that candle COMPLETES next cycle — exits are completed-bar, like triggers.
    df = _frame_with_lows(completed_low=100.0, forming_low=94.0, last_open=_LAST_OPEN)
    ctx = fetch_context(FakeExchange({"BTC/USDT:USDT": df}), _settings())
    report = {"carried": 0, "closed": 0, "actions": []}
    still = audit_and_reflect(ctx, [_long_pos()], AccountState(balance=10_000.0,
                              peak_equity=10_000.0), tmp_path / "m", _NOW, report)
    assert report["closed"] == 0
    assert len(still) == 1

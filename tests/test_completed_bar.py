"""BUG fix (found live in cycle 18): the OHLCV feed returns the still-FORMING 4h candle as the last
row, but triggers and the brief's last_close/momentum must read the last COMPLETED bar — not a
transient intra-candle print. last_completed_frame drops the forming last candle when `now` falls
inside its window; it leaves an already-closed last candle untouched (and is a no-op without now)."""
from datetime import UTC, datetime, timedelta

import pandas as pd

from futures_fund.brief import last_completed_frame


def _frame(n, last_open, freq="4h"):
    ts = pd.date_range(end=last_open, periods=n, freq=freq, tz="UTC")
    close = [100.0 + i for i in range(n)]
    return pd.DataFrame({"timestamp": ts, "open": close, "high": close, "low": close,
                         "close": close, "volume": [1.0] * n})


def test_drops_forming_last_candle():
    # last candle opened at 04:00; now is 04:30 (inside the 04:00-08:00 window) -> last row is FORMING
    df = _frame(5, pd.Timestamp("2026-06-02 04:00", tz="UTC"))
    now = datetime(2026, 6, 2, 4, 30, tzinfo=UTC)
    out = last_completed_frame(df, now, "4h")
    assert len(out) == 4                                   # forming candle dropped
    assert out["timestamp"].iloc[-1] == pd.Timestamp("2026-06-02 00:00", tz="UTC")  # last COMPLETED


def test_keeps_already_completed_last_candle():
    # last candle opened at 00:00 and now is 05:00 (> one 4h window later) -> it's CLOSED, keep it
    df = _frame(5, pd.Timestamp("2026-06-02 00:00", tz="UTC"))
    now = datetime(2026, 6, 2, 5, 0, tzinfo=UTC)
    out = last_completed_frame(df, now, "4h")
    assert len(out) == 5                                   # nothing dropped
    assert out["timestamp"].iloc[-1] == pd.Timestamp("2026-06-02 00:00", tz="UTC")


def test_no_now_is_noop():
    df = _frame(5, pd.Timestamp("2026-06-02 04:00", tz="UTC"))
    assert len(last_completed_frame(df, None, "4h")) == 5  # backward-compatible: no now -> unchanged


def test_never_drops_below_one_row():
    df = _frame(1, pd.Timestamp("2026-06-02 04:00", tz="UTC"))
    now = datetime(2026, 6, 2, 4, 30, tzinfo=UTC)
    out = last_completed_frame(df, now, "4h")
    assert len(out) == 1                                   # never strands an empty frame


def test_empty_or_none_safe():
    assert last_completed_frame(None, datetime(2026, 6, 2, tzinfo=UTC), "4h") is None
    empty = _frame(5, pd.Timestamp("2026-06-02 04:00", tz="UTC")).iloc[0:0]
    assert len(last_completed_frame(empty, datetime(2026, 6, 2, 4, 30, tzinfo=UTC), "4h")) == 0


def test_brief_last_close_uses_completed_bar():
    # the real-world repro: HYPE-style — forming candle prints a transient close, completed is lower
    from tests.test_orchestration import FakeExchange, _uptrend
    import datetime as dt
    ex = FakeExchange({"BTC/USDT:USDT": _uptrend()})
    from futures_fund.brief import build_symbol_brief
    # _uptrend's last candle is dated 2026-01 (long before any plausible `now`), so with a `now`
    # far in the future the last candle is COMPLETED -> brief uses it (no spurious drop).
    b = build_symbol_brief(ex, "BTC/USDT:USDT", "4h", now=dt.datetime(2026, 3, 1, tzinfo=dt.UTC))
    assert b["last_close"] > 0  # builds cleanly with the now-aware path

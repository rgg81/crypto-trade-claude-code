"""Tests for the missed-candle replay aggregate window (futures_fund.replay.gap_window).

On resume after a loop outage the exit check must consider EVERY completed candle since the
last-served candle, not just the latest, so a stop/TP/liq touched during a missed candle is honored.
`gap_window` returns the (max_high, min_low, gap_open) over the missed bars; the existing
detect_exit + pessimistic priority then fill it conservatively. The crux is the off-by-one: the bar
opening AT the prior served candle was still FORMING during the prior run and was never evaluated,
so the window floor is `open-ts >= last_served_ts` (`>` would skip that first missed candle).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd

from futures_fund.replay import gap_window

UTC = timezone.utc
H4 = timedelta(hours=4)
T0 = datetime(2026, 3, 1, 0, 0, tzinfo=UTC)  # bar-0 open-ts; bars step 4h from here


def _frame(bars, start=T0):
    """bars: list of (open, high, low). Bar i opens at start + i*4h (timestamps step 4h)."""
    ts = pd.date_range(start=start, periods=len(bars), freq="4h", tz="UTC")
    return pd.DataFrame({
        "timestamp": ts,
        "open": [b[0] for b in bars],
        "high": [b[1] for b in bars],
        "low": [b[2] for b in bars],
        "close": [b[0] for b in bars],
        "volume": [1.0] * len(bars),
    })


# Frame of 5 bars (t0..t4); `now` sits in t4's window so t4 is the FORMING bar (dropped) and t3 is
# the latest COMPLETED bar. Distinct per-bar highs/lows so the aggregate is unambiguous.
def _frame5():
    return _frame([
        (100.0, 101.0, 99.0),    # t0
        (100.0, 102.0, 98.0),    # t1
        (100.0, 200.0, 50.0),    # t2  <- extreme high/low (the off-by-one canary)
        (100.0, 110.0, 90.0),    # t3  <- latest COMPLETED
        (100.0, 999.0, 1.0),     # t4  <- FORMING (must be dropped, never aggregated)
    ])


NOW = T0 + 4 * H4  # inside t4's window -> t4 forming, t3 latest completed


def test_no_gap_returns_single_latest_bar():
    # prior served candle = t3 (one candle back) -> window = {t3} = the single new completed bar.
    win = gap_window(_frame5(), T0 + 3 * H4, NOW)
    assert win == (110.0, 90.0, 100.0)  # t3's (high, low, open) -> identical to today's single bar


def test_one_candle_gap_includes_the_bar_that_opened_at_the_served_candle():
    # THE OFF-BY-ONE PIN. prior served candle = t2; an outage skipped t3's candle. The bar that
    # opened AT t2 was forming during the prior run -> never evaluated -> MUST be in the window.
    win = gap_window(_frame5(), T0 + 2 * H4, NOW)
    # window = {t2, t3}: max_high from t2 (200), min_low from t2 (50), gap_open = t2's open (100).
    assert win == (200.0, 50.0, 100.0)
    # '>' (the buggy floor) would yield only {t3} -> (110, 90, ...), missing t2's 50 low.


def test_three_bar_gap_aggregates_all_missed_bars():
    f = _frame([
        (10.0, 11.0, 9.0),    # t0
        (20.0, 25.0, 18.0),   # t1  <- earliest in window; opens are uniform so gap_open = 20
        (20.0, 30.0, 12.0),   # t2  <- min_low here (12)
        (20.0, 40.0, 19.0),   # t3  <- max_high here (40), latest COMPLETED
        (20.0, 99.0, 0.5),    # t4  <- FORMING (dropped)
    ])
    win = gap_window(f, T0 + 1 * H4, NOW, direction="long")  # window = {t1, t2, t3}
    assert win == (40.0, 12.0, 20.0)


def test_long_gap_open_is_the_lowest_window_open_not_the_earliest():
    # CONSERVATISM: a long stop fills at min(level, open), so the gap-open MUST be the LOWEST open
    # in the window — the bar that gapped DOWN furthest through the stop — even when that bar is
    # LATER than the earliest missed bar. The earliest open (100 here) would be OPTIMISTIC.
    f = _frame([
        (10.0, 11.0, 9.0),       # t0
        (100.0, 101.0, 96.0),    # t1  earliest in window, open 100 (sits ABOVE a ~95 stop)
        (90.0, 99.0, 85.0),      # t2  LATER bar gaps DOWN, open 90 (BELOW the stop) -> honest fill
        (110.0, 112.0, 108.0),   # t3  latest completed, open 110
        (110.0, 999.0, 1.0),     # t4  forming (dropped)
    ])
    _, _, gap_open = gap_window(f, T0 + 1 * H4, NOW, direction="long")
    assert gap_open == 90.0      # min open; the earliest-open (100) would be optimistically high


def test_short_gap_open_is_the_highest_window_open_not_the_earliest():
    # Mirror: a short stop fills at max(level, open) -> the gap-open is the HIGHEST window open.
    f = _frame([
        (10.0, 11.0, 9.0),       # t0
        (100.0, 104.0, 99.0),    # t1  earliest, open 100 (sits BELOW a ~105 stop)
        (110.0, 120.0, 109.0),   # t2  LATER bar gaps UP, open 110 (ABOVE the stop) -> honest fill
        (90.0, 92.0, 88.0),      # t3  latest completed, open 90
        (90.0, 999.0, 1.0),      # t4  forming (dropped)
    ])
    _, _, gap_open = gap_window(f, T0 + 1 * H4, NOW, direction="short")
    assert gap_open == 110.0     # max open; the earliest-open (100) would be optimistically low


def test_none_floor_returns_latest_single_bar():
    win = gap_window(_frame5(), None, NOW)
    assert win == (110.0, 90.0, 100.0)  # cold start -> today's single-bar behavior


def test_stale_floor_after_latest_completed_falls_back_to_latest_single_bar():
    # last_served_ts AT/after the latest completed bar (clock skew) -> no bars qualify -> latest.
    win = gap_window(_frame5(), T0 + 4 * H4, NOW)  # floor = t4 (the forming bar's open) > t3
    assert win == (110.0, 90.0, 100.0)


def test_forming_bar_is_dropped_never_aggregated():
    # t4's 999 high / 1.0 low must NEVER leak into the window even with a wide floor.
    win = gap_window(_frame5(), T0, NOW)  # floor = t0 -> window = {t0..t3}, NOT t4
    max_high, min_low, _ = win
    assert max_high == 200.0 and min_low == 50.0  # from t2; t4's 999/1.0 excluded


def test_empty_frame_returns_none():
    assert gap_window(_frame([]), T0, NOW) is None


def test_none_frame_returns_none():
    assert gap_window(None, T0, NOW) is None


def test_single_row_frame_is_safe():
    # one row, no `now`-drop possible -> that row's (high, low, open).
    win = gap_window(_frame([(5.0, 6.0, 4.0)]), None, NOW)
    assert win == (6.0, 4.0, 5.0)


def test_naive_floor_does_not_crash():
    # a tz-naive floor (defensive) is coerced to UTC, not compared raw against tz-aware bars.
    naive = datetime(2026, 3, 1, 8, 0)  # == T0 + 2*H4, naive
    win = gap_window(_frame5(), naive, NOW)
    assert win == (200.0, 50.0, 100.0)  # same as the tz-aware t2 floor

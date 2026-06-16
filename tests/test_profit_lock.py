"""Fire-time profit-lock ladder (#268) — deterministic, up-only profit-lock stop computation.

The gate applies this every cycle a position is open (incl. the fire cycle), ratcheting the stop
toward profit based on the position's R-multiple favorable excursion — no LLM. R-units are anchored
to the ORIGINAL risk (|entry - entry_stop|) so the ladder is stable after the live stop ratchets.
The wiring (cycle.py) only ever applies the result through the existing tighten-only rule.
"""
import math

from futures_fund.profit_lock import (
    LADDER_RUNGS,
    is_tighter_stop,
    ladder_stop,
    ratcheted_stop,
)

# A realistic long: BTC cy97 — entry 66495.9, original stop 66046.8 -> R_unit = 449.1.
ENTRY = 66495.9
ESTOP = 66046.8
R = ENTRY - ESTOP  # 449.1


def _long(fav):
    return ladder_stop("long", ENTRY, ESTOP, fav)


def test_below_first_rung_returns_none():
    # +0.9R favorable — no rung reached
    assert _long(ENTRY + 0.9 * R) is None


def test_first_rung_locks_breakeven():
    # +1.0R -> stop to breakeven (entry)
    assert _long(ENTRY + 1.0 * R) == ENTRY


def test_second_rung_locks_half_R():
    assert _long(ENTRY + 1.5 * R) == ENTRY + 0.5 * R


def test_third_rung_locks_one_R():
    assert _long(ENTRY + 2.0 * R) == ENTRY + 1.0 * R


def test_fourth_rung_locks_two_R():
    assert _long(ENTRY + 3.0 * R) == ENTRY + 2.0 * R


def test_above_top_rung_caps_at_top():
    # +5R is past the top rung (+3R->+2R) — caps at the top rung, never exceeds it
    assert _long(ENTRY + 5.0 * R) == ENTRY + 2.0 * R


def test_btc_cy97_scenario_locks_half_r():
    # the firing candle high ~67255 = +1.69R -> the +1.5R rung -> stop +0.5R = 66720.45
    fav = 67255.0
    assert abs(_long(fav) - (ENTRY + 0.5 * R)) < 1e-9


def test_monotonic_up_only():
    # higher favorable excursion never yields a LOWER stop
    prev = -math.inf
    for mult in (1.0, 1.2, 1.5, 1.9, 2.0, 2.5, 3.0, 4.0):
        s = _long(ENTRY + mult * R)
        assert s is not None and s >= prev
        prev = s


def test_short_mirror_half_R():
    # short: entry 100, original stop 110 -> R_unit 10; +1.5R favorable = price 85 -> stop 95
    s = ladder_stop("short", 100.0, 110.0, 85.0)
    assert s == 95.0


def test_short_below_first_rung_none():
    assert ladder_stop("short", 100.0, 110.0, 91.0) is None  # +0.9R


def test_degenerate_r_unit_returns_none():
    # entry == entry_stop -> R_unit 0 -> None (no divide-by-zero, no ratchet)
    assert ladder_stop("long", 100.0, 100.0, 200.0) is None


def test_non_finite_favorable_returns_none():
    assert _long(float("inf")) is None
    assert _long(float("nan")) is None


def test_default_rungs_are_nonnegative_lock_and_sorted():
    # the default ladder never proposes a LOSING stop, and rungs are ascending by trigger
    triggers = [t for t, _ in LADDER_RUNGS]
    assert triggers == sorted(triggers)
    assert all(lock >= 0.0 for _, lock in LADDER_RUNGS)
    assert all(lock <= trig for trig, lock in LADDER_RUNGS)  # never lock more than the excursion


# --- tighten-only rule (the canonical safety primitive; the ladder wiring applies through it) ---

def test_tighter_long_accepts_higher_stop_short_of_mark():
    # long: cur < new < mark  -> tighter (locks more profit)
    assert is_tighter_stop("long", 95.0, 98.0, 100.0) is True


def test_tighter_long_rejects_looser_or_past_mark():
    assert is_tighter_stop("long", 98.0, 95.0, 100.0) is False   # lower (looser) -> reject
    assert is_tighter_stop("long", 95.0, 101.0, 100.0) is False  # past mark -> reject (insta-stop)
    assert is_tighter_stop("long", 95.0, 95.0, 100.0) is False   # equal -> not strictly tighter


def test_tighter_short_mirror():
    assert is_tighter_stop("short", 105.0, 102.0, 100.0) is True   # mark < new < cur -> tighter
    assert is_tighter_stop("short", 102.0, 105.0, 100.0) is False  # higher (looser) -> reject
    assert is_tighter_stop("short", 105.0, 99.0, 100.0) is False   # past mark -> reject


def test_tighter_none_or_nonfinite_mark_is_false():
    assert is_tighter_stop("long", 95.0, 98.0, None) is False
    assert is_tighter_stop("long", 95.0, 98.0, float("nan")) is False


# --- ratcheted_stop: the bar-level combinator the gate calls per open position ---

def test_ratcheted_stop_btc_fire_candle_locks_half_r():
    # the cy97 BTC fire candle: opened ~66192, ran through entry, high 67255 (+1.69R), close 67248.
    # cur_stop = original 66046.8. Ladder unlocks the +1.5R rung -> stop +0.5R = 66720.45, and it is
    # tighter (66046.8 < 66720.45) and short of the close 67248 -> ratchets.
    new = ratcheted_stop("long", ENTRY, ESTOP, ESTOP, bar_high=67255.0, bar_low=66192.0,
                         bar_close=67248.0)
    assert abs(new - (ENTRY + 0.5 * R)) < 1e-9


def test_ratcheted_stop_none_when_not_in_profit():
    # a bar that never reached +1R: high only +0.5R -> no rung -> None (no ratchet)
    new = ratcheted_stop("long", ENTRY, ESTOP, ESTOP,
                         bar_high=ENTRY + 0.5 * R, bar_low=ENTRY - 0.2 * R, bar_close=ENTRY)
    assert new is None


def test_ratcheted_stop_none_when_not_tighter():
    # ladder proposes breakeven (+1R rung) but cur_stop is ALREADY above it -> not tighter -> None
    cur = ENTRY + 0.3 * R   # already trailed past breakeven
    new = ratcheted_stop("long", ENTRY, ESTOP, cur,
                         bar_high=ENTRY + 1.0 * R, bar_low=cur + 1.0, bar_close=ENTRY + 0.8 * R)
    assert new is None  # +1R rung -> breakeven (entry) < cur -> not tighter


def test_ratcheted_stop_rejects_when_close_below_candidate():
    # high spiked to +2R (rung -> +1R stop) but the candle CLOSED below that lock -> not short of
    # mark -> rejected (no optimistic lock above the close)
    new = ratcheted_stop("long", ENTRY, ESTOP, ESTOP,
                         bar_high=ENTRY + 2.0 * R, bar_low=ENTRY, bar_close=ENTRY + 0.4 * R)
    assert new is None  # candidate +1R (entry+R) is NOT < close (entry+0.4R) -> rejected


def test_ratcheted_stop_short_mirror():
    # short entry 100 / original stop 110 (R 10); bar low 84 (+1.6R), close 86 -> +1.5R rung -> 95
    new = ratcheted_stop("short", 100.0, 110.0, 110.0, bar_high=101.0, bar_low=84.0, bar_close=86.0)
    assert new == 95.0

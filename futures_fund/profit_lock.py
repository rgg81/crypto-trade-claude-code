"""Fire-time profit-lock ladder (#268) — deterministic, up-only profit-lock stop computation.

The gate (`cycle.py`) calls `ladder_stop` for every open position every cycle it runs (including the
fire cycle) and applies the result ONLY through the existing tighten-only rule, so a freshly-fired
deep-in-profit position is protected without waiting for an LLM management cycle. Pure, no IO.

R-units are anchored to the ORIGINAL per-unit risk (`|entry - entry_stop|`), NOT the live (possibly
already-ratcheted) stop, so the ladder is stable across ratchets. The default rungs are conservative
(room early, lock progressively) and never propose a losing stop (all `lock_R >= 0`).
"""
from __future__ import annotations

import math

# Ordered (trigger_R, lock_R) rungs: once favorable excursion reaches trigger_R, lock the stop at
# lock_R of profit. Conservative — give the trade room early, ratchet profit progressively.
LADDER_RUNGS: list[tuple[float, float]] = [
    (1.0, 0.0),   # +1.0R reached -> stop to breakeven (entry)
    (1.5, 0.5),   # +1.5R reached -> stop to +0.5R
    (2.0, 1.0),   # +2.0R reached -> stop to +1.0R
    (3.0, 2.0),   # +3.0R reached -> stop to +2.0R
]


def is_tighter_stop(direction: str, cur_stop: float, new_stop: float, mark: float | None) -> bool:
    """The canonical tighten-only stop rule (the safety primitive). A trailed/ratcheted stop is
    valid ONLY if it is STRICTLY tighter than the current stop AND short of the mark: a winning long
    locks profit ABOVE the old stop but BELOW the mark, a winning short BELOW the old stop but ABOVE
    the mark (a stop past the mark would insta-stop). Returns False on a missing/non-finite mark
    (cannot validate -> never loosen, never act). Shared by the profit-lock ladder (cycle.py) and
    the LLM HOLD/reduce trail (orchestration.py) so there is ONE definition of 'tighter'."""
    if mark is None:
        return False
    try:
        m = float(mark)
    except (TypeError, ValueError):
        return False
    if not math.isfinite(m):
        return False
    return ((direction == "long" and cur_stop < new_stop < m) or
            (direction == "short" and m < new_stop < cur_stop))


def ladder_stop(
    direction: str,
    entry: float,
    entry_stop: float,
    favorable_price: float,
    rungs: list[tuple[float, float]] = LADDER_RUNGS,
) -> float | None:
    """The highest profit-lock stop the favorable excursion has UNLOCKED, or None.

    `favorable_price` is the bar's most-favorable price (high for a long, low for a short). Returns
    None when no rung is reached, the original risk is degenerate (R_unit <= 0), or the favorable
    price is non-finite. Up-only by construction (a higher favorable excursion selects an
    equal-or-higher rung). The caller applies the result through the tighten-only rule."""
    r_unit = abs(entry - entry_stop)
    if r_unit <= 0.0 or not math.isfinite(favorable_price):
        return None
    if direction == "long":
        fav_r = (favorable_price - entry) / r_unit
    else:
        fav_r = (entry - favorable_price) / r_unit
    lock_r: float | None = None
    for trigger_r, rung_lock_r in rungs:
        if fav_r >= trigger_r:
            lock_r = rung_lock_r  # rungs ascending -> last match is the highest unlocked
    if lock_r is None:
        return None
    return entry + lock_r * r_unit if direction == "long" else entry - lock_r * r_unit


def ratcheted_stop(
    direction: str,
    entry: float,
    entry_stop: float,
    cur_stop: float,
    bar_high: float,
    bar_low: float,
    bar_close: float,
) -> float | None:
    """The new (TIGHTER) profit-lock stop for one bar, or None if no ratchet applies — the bar-level
    combinator the gate calls per open position. Favorable excursion is the bar HIGH (long) / LOW
    (short); the candidate is accepted ONLY via the tighten-only rule with the bar CLOSE as mark.
    Using the close as mark keeps the ratchet conservative (it never locks a stop ABOVE the bar's
    close), and the gate applies it at cycle-end so the exit check always lags the ratchet by one
    cycle (no intra-candle high-before-low optimism)."""
    favorable = bar_high if direction == "long" else bar_low
    candidate = ladder_stop(direction, entry, entry_stop, favorable)
    if candidate is not None and is_tighter_stop(direction, cur_stop, candidate, bar_close):
        return candidate
    return None

"""Missed-candle replay (aggregate-window, exits-only) — the deferred Problem-B sibling of #268.

The exit audit reads exactly one bar (the latest completed 4h candle). During a loop outage the gate
does not run for several boundaries; on resume it would otherwise serve only the LATEST candle and
silently discard the intermediate MISSED candles, so a stop/TP/liq that price touched during a
missed candle goes unhonored when price recovered by the latest bar. `gap_window` collapses every
completed candle since the last-served candle into a single conservative (max_high, min_low,
gap_open) window; the EXISTING `detect_exit` + pessimistic priority (liq > stop > tp) then fill it
exactly as today. The change can only WIDEN the [low, high] checked — it can surface a missed exit,
never suppress one — and the gap-honest fill stays PESSIMISTIC: gap_open is the directionally-worst
open in the window (min for a long, max for a short), never the earliest. Pure, no IO.

Off-by-one (the crux): a cycle that ran at instant `T` evaluated the bar with open-ts `floor4(T)-4h`
— `last_completed_frame` drops the then-forming served-candle bar (open-ts `floor4(T)`). The served
candle stamped in its report is `S = floor4(T)`, so the bar opening EXACTLY at `S` was still forming
during that run and was never evaluated: it is the FIRST missed bar. The window floor is therefore
`open-ts >= last_served_ts`; `>` would skip that first missed candle. No-gap reduces to the single
latest bar (`S` one step back selects exactly the one new completed bar) — byte-identical to today.
"""
from __future__ import annotations

from datetime import UTC, datetime

from futures_fund.brief import last_completed_frame


def _aware_utc(ts) -> datetime | None:
    """Coerce a frame timestamp (pandas Timestamp / datetime, possibly tz-naive) to aware-UTC, or
    None if not a datetime. Naive is treated as already-UTC (matching last_completed_frame)."""
    try:
        ts = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
        if not isinstance(ts, datetime):
            return None
        return ts.replace(tzinfo=UTC) if ts.tzinfo is None else ts.astimezone(UTC)
    except Exception:  # noqa: BLE001 — never break the exit path over a timestamp coercion
        return None


def gap_window(df, last_served_ts: datetime | None, now: datetime | None,
               timeframe: str = "4h", direction: str = "long"):
    """The (max_high, min_low, gap_open) over the COMPLETED candles the gate missed during a gap —
    those with open-ts `>= last_served_ts`, up to the latest completed bar.

    `gap_open` is the DIRECTIONALLY-CONSERVATIVE open for a gap-honest stop fill, NOT the earliest
    bar's open. A long stop fills at `min(level, open)`, so the most pessimistic open is the MINIMUM
    across the window — the bar that gapped DOWN furthest through the stop, which may be a LATER bar
    than the earliest missed one; a short stop fills at `max(level, open)`, so it is the MAXIMUM.
    Returning the earliest open would pin the fill to the unreachable stop LEVEL when a later bar is
    the one that gapped — booking a smaller loss than reality (an optimistic paper win). Any open
    below a long's stop belongs to a bar that genuinely gapped through it, so the min-open is the
    honest worst-case fill, never an over-penalty.

    Returns the SINGLE latest completed bar's (high, low, open) when there is no gap (one new bar),
    when `last_served_ts` is None (cold start) or stale (>= the latest completed open-ts, e.g. clock
    skew), or when the frame is too short — so the normal cadence is byte-identical to today. None
    only when there is no completed bar at all (empty/None frame). The still-forming last bar is
    dropped (via `last_completed_frame`) and never aggregated. Pure, no IO."""
    cdf = last_completed_frame(df, now, timeframe)
    if cdf is None or not len(cdf):
        return None
    latest = cdf.iloc[-1]
    latest_tuple = (float(latest["high"]), float(latest["low"]), float(latest["open"]))
    if last_served_ts is None:
        return latest_tuple
    floor = _aware_utc(last_served_ts)
    if floor is None:
        return latest_tuple  # un-parseable floor -> single bar (fail-safe, today's behavior)
    rows = [i for i in range(len(cdf))
            if (t := _aware_utc(cdf["timestamp"].iloc[i])) is not None and t >= floor]
    if not rows:
        return latest_tuple  # stale / future floor -> latest single bar (fail-safe)
    highs = [float(cdf["high"].iloc[i]) for i in rows]
    lows = [float(cdf["low"].iloc[i]) for i in rows]
    opens = [float(cdf["open"].iloc[i]) for i in rows]
    gap_open = min(opens) if direction == "long" else max(opens)  # most pessimistic gap-honest fill
    return (max(highs), min(lows), gap_open)

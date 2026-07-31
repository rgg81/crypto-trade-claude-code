"""Due-gate for the hourly-poll loop: decide whether THIS 4h candle still needs a cycle.

The desk wants exactly one cycle per 4h Binance candle (UTC grid 00/04/08/12/16/20). The
session-only cron fires only while the REPL is idle, so a tick landing mid-cycle is skipped and
never replays. To make a skipped boundary self-heal, the cron polls HOURLY and gates here:

    run iff no completed cycle has yet SERVED the candle that contains `now`.

Design notes (vetted by the design red-team, see tests/test_scheduling.py):
  * The cadence primitive is the SERVED CANDLE — report['candle'] = floor4(gate-start instant) —
    NOT completion time. A catch-up that finishes after the next boundary still only serves the
    candle it started in, so it cannot "steal" the next candle.
  * "Last completed cycle" = the highest cycle number whose report.json EXISTS and PARSES, found
    by scanning dirs in DESCENDING order. Never max(dir): a phantom empty dir or a crashed
    pre-gate dir must not wedge the loop into permanent SKIP.
  * All datetimes are tz-aware UTC end to end. mtime fallback uses fromtimestamp(ts, tz=UTC);
    ran_at/candle parsing normalizes 'Z' and coerces any naive value to UTC. floor4 asserts aware.
  * Fail-safe: any unhandled error returns DUE (an extra run is low-harm — the gate reconciles
    against on-disk positions and cannot double-open — whereas a swallowed candle is worse).

Returns (mode, n, reason):
  mode == 'FRESH'  -> run a brand-new cycle, create state/cycle/<n>/ (n = highest_dir + 1)
  mode == 'RETRY'  -> re-run/overwrite the crashed dir state/cycle/<n>/ (n = highest_dir)
  mode == 'SKIP'   -> this candle is already served; do nothing (n = the serving cycle)
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

UTC = timezone.utc
# CADENCE of the full funnel, in hours (cy313: 4 -> 8, user-directed, to halve the token cost of a
# team run). This is NOT the data timeframe — see floor_cycle. Must divide 24 so the grid is stable
# across days. Every candle-grid consumer derives from this single constant so they cannot drift.
CYCLE_HOURS = 8
_CANDLE = timedelta(hours=CYCLE_HOURS)
# Tolerate a served candle up to ONE step ahead of now's boundary, then distrust as corrupt.
# WHY: under a correct monotonic clock a served candle is always <= now's boundary
# (candle = floor4(start) <= floor4(now)), so this tolerance is dormant in normal operation. It
# only engages on a clock anomaly. A sub-candle backward NTP step across a boundary makes the
# JUST-served candle look one step ahead; trusting it yields a bounded SKIP (correct — don't
# re-serve it) instead of a needless re-run. COST: a LARGER (>=4h) backward step or a >=4h forward
# write-skew that survives correction can false-SKIP and swallow up to two real candles before it
# self-clears. That is an accepted, bounded, self-healing tradeoff for a paper desk; tighten this
# toward a few minutes if even that bounded swallow is unacceptable (then re-derive the
# clock_moved_backward test, whose backstep would flip to a harmless DUE re-run).
_FUTURE_TOL = _CANDLE


def floor_cycle(dt: datetime) -> datetime:
    """Floor a tz-aware UTC datetime to the CYCLE grid (CYCLE_HOURS=8 -> 00/08/16 UTC).

    NOTE this is the CADENCE grid — how often the full funnel runs — NOT the DATA timeframe, which
    stays 4h (`settings.timeframe`): the briefs, ATR/ADX/swings, exits and trigger fires all still
    read 4h bars. cy313 moved the cadence 4h -> 8h to halve the token cost of a full team run. That
    is only safe because BOTH bar-consuming paths read every completed candle since the last-served
    one rather than just the latest: exits via `replay.gap_window`, and the fire path via the `seq`
    window built in `orchestration.gate_execute_step`. Keep those two properties if this changes."""
    assert dt.tzinfo is not None, "floor_cycle requires a tz-aware datetime"
    return dt.replace(hour=(dt.hour // CYCLE_HOURS) * CYCLE_HOURS,
                      minute=0, second=0, microsecond=0)


floor4 = floor_cycle   # back-compat alias for existing call sites (gate_execute_cli, regime)


def _parse_utc(raw) -> datetime | None:
    """Parse an ISO timestamp to an aware-UTC datetime, or None. Never raises."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        dt = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    # Deliver UTC as the docstring promises: normalize any foreign offset (e.g. +05:30) to UTC,
    # and treat a naive stamp as already-UTC. Either way floor4 then sees a true-UTC instant.
    return dt.astimezone(UTC) if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _served_candle(report_path: Path, now_utc: datetime) -> datetime | None:
    """Resolve which candle a completed cycle served, from its report.json. Priority:
    report['candle'] -> floor4(report['ran_at']) -> floor4(file mtime). All tz-aware UTC.
    A ran_at in the future (clock skew) is discarded so it cannot drive the candle. Returns None
    if the report cannot be read/parsed (caller treats that dir as not-completed)."""
    try:
        rep = json.loads(report_path.read_text())
    except (json.JSONDecodeError, OSError, ValueError):
        return None
    if not isinstance(rep, dict):
        return None  # valid JSON but not an object (null/list/scalar) == not a completed cycle
    ran_at = _parse_utc(rep.get("ran_at"))
    if ran_at is not None and ran_at > now_utc:
        ran_at = None  # future-stamp guard: never let a skewed ran_at wedge the loop
    cand = _parse_utc(rep.get("candle"))
    if cand is None and ran_at is not None:
        cand = floor4(ran_at)
    if cand is None:
        try:
            cand = floor4(datetime.fromtimestamp(report_path.stat().st_mtime, tz=UTC))
        except OSError:
            return None
    return cand


def last_served_candle(state_dir, now_utc: datetime) -> datetime | None:
    """The served candle (report['candle'] = floor4(gate-start)) of the most-recent COMPLETED cycle
    — the highest cycle dir whose report.json parses, found by the SAME descending scan +
    `_served_candle` as `cycle_due`. This is the FLOOR of the missed-candle gap window
    (futures_fund.replay): the last candle the gate processed before this run. As the current cycle
    audits exits its own report.json does not yet exist, so this returns the PRIOR cycle's candle —
    exactly the gap floor; a RETRY (no/garbled report on the current dir) is skipped, so the retry
    re-processes the same floor idempotently. Returns None on cold start or any error (fail-safe:
    the caller then checks only the latest single bar = today's behavior). Never raises."""
    try:
        cyc = Path(state_dir) / "cycle"
        if not cyc.exists():
            return None
        dirs = sorted(
            (int(p.name) for p in cyc.glob("*") if p.is_dir() and p.name.isdigit()),
            reverse=True,
        )
        for n in dirs:
            rp = cyc / str(n) / "report.json"
            if not rp.exists():
                continue  # crashed/in-flight (incl. THIS cycle pre-report): not a completed cycle
            cand = _served_candle(rp, now_utc)
            if cand is not None:
                return cand
        return None
    except Exception:  # noqa: BLE001 — fail-safe: no floor -> single-bar check (today's behavior)
        return None


def cycle_due(state_dir, now_utc: datetime) -> tuple[str, int, str]:
    """Decide whether the candle containing `now_utc` still needs a cycle. Never raises."""
    try:
        assert now_utc.tzinfo is not None and now_utc.utcoffset() == timedelta(0), \
            "now_utc must be tz-aware UTC"
        boundary = floor4(now_utc)
        cyc = Path(state_dir) / "cycle"

        dirs = sorted(
            (int(p.name) for p in cyc.glob("*") if p.is_dir() and p.name.isdigit()),
            reverse=True,
        ) if cyc.exists() else []
        if not dirs:
            return ("FRESH", 1, "cold-start: no cycle dirs")
        highest_dir = dirs[0]

        completed_n: int | None = None
        served: datetime | None = None
        for n in dirs:
            rp = cyc / str(n) / "report.json"
            if not rp.exists():
                continue  # crashed/in-flight: not a completed cycle
            cand = _served_candle(rp, now_utc)
            if cand is None:
                continue  # unparseable report == not completed
            if cand > boundary + _FUTURE_TOL:
                continue  # egregiously-future candle (corrupt/skew) -> distrust, scan downward
            completed_n, served = n, cand
            break

        if completed_n is None or served is None:
            # No trustworthy completed cycle. The highest dir is a crashed/junk attempt -> RETRY it
            # (overwrite). Safe: the gate reconciles vs on-disk positions and cannot double-open.
            return ("RETRY", highest_dir, f"no completed cycle; retry/overwrite dir {highest_dir}")

        if served >= boundary:
            nxt = (boundary + _CANDLE).isoformat()
            return ("SKIP", completed_n,
                    f"cycle {completed_n} already served candle {served.isoformat()} "
                    f"(>= boundary {boundary.isoformat()}); next boundary {nxt}")

        # This candle is unserved -> DUE. If a higher dir exists with no trustworthy report, it is
        # a crashed current-candle attempt -> RETRY/overwrite it; otherwise a FRESH next cycle.
        if highest_dir > completed_n:
            return ("RETRY", highest_dir,
                    f"cycle {highest_dir} crashed before gate; last completed {completed_n} "
                    f"served {served.isoformat()}")
        return ("FRESH", highest_dir + 1,
                f"new candle {boundary.isoformat()}; last completed {completed_n} "
                f"served {served.isoformat()}")
    except Exception as e:  # noqa: BLE001 — fail SAFE: never swallow a candle on an internal error
        return ("FRESH", 1, f"fail-safe DUE after internal error: {e!r}")

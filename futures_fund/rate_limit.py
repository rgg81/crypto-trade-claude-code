"""Binance IP-weight HEADROOM guard — ask "is there room?", not "is there a ban?".

THE BUG THIS FIXES (cy317, 2026-08-01, the 4th 418 event in ~8 days). The standing
discipline was to probe with a lightweight `fetch_time()` (weight ~1) before the heavy
scout and stop on a 418. That probe PASSED at 11:04:51Z and the very next call — the
scout's `load_markets()` — took a FRESH ban (`IP banned until 11:25:44Z`). Because the
probe succeeded, the ban was NOT already active: the scout's own burst created it.

    A weight-1 probe can only detect an ALREADY-ACTIVE ban. It is blind to a
    DEPLETED-WEIGHT state — which is precisely the condition under which the next
    heavy call trips a new ban. "Probe passed" is therefore a FALSE NEGATIVE.

Live sampling right after the ban lapsed made the cause concrete: the used-weight counter
swung 1405 -> 613 -> 1264 -> 362 -> 1170 across ~75s while this process issued only
weight-1 calls. Something ELSE is consuming this IP's 2400/min budget — confirming, with
numbers, the shared-IP/pool hypothesis first raised at cy292 (when a fresh post-reboot IP
arrived already banned). We cannot control the co-consumer and we cannot dodge it by
cycling IPs; we can only decline to spend a burst into an already-drained window.

Binance returns the budget on EVERY response (`X-MBX-USED-WEIGHT-1M`), so reading it costs
nothing extra. This module turns that header into a go/no-go, and it is FAIL-CLOSED: an
absent or unparseable header is treated as "no headroom proven", never as zero usage.

Read-only with respect to desk state; makes only weight-1 calls of its own.
"""
from __future__ import annotations

import time
from collections.abc import Callable

# Binance USD-M futures IP weight cap, per minute, per IP.
WEIGHT_LIMIT_PER_MIN = 2400

# Launch a heavy burst only well under the cap. The scout (load_markets + fetch_tickers)
# costs on the order of a few dozen weight, but the co-consumer's swing is ~1000 within a
# single window, so the margin has to absorb IT, not just us.
DEFAULT_THRESHOLD = 700

_HEADER = "x-mbx-used-weight-1m"


class HeadroomTimeout(RuntimeError):
    """No sufficiently-clear weight window appeared within the allotted polls."""


def used_weight(client) -> int | None:
    """The IP weight consumed in the current 1-minute window, per the LAST response.

    Returns None when the header is absent or unparseable — the caller MUST treat that as
    "unknown", never as zero. Reading is case-insensitive (Binance's casing varies).
    """
    headers = getattr(client, "last_response_headers", None)
    if not headers:
        return None
    try:
        for k, v in headers.items():
            if k.lower() == _HEADER:
                return int(v)
    except (AttributeError, TypeError, ValueError):
        return None
    return None


def wait_for_headroom(
    client,
    *,
    threshold: int = DEFAULT_THRESHOLD,
    max_polls: int = 12,
    poll_seconds: float = 12.0,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Poll a weight-1 endpoint until the IP's used weight is under `threshold`.

    Returns the observed used weight once a clear window is found. Raises HeadroomTimeout
    if none appears — fail-LOUD, so the caller surfaces a blocked cycle rather than firing
    a burst into a drained budget and earning a 20-50 minute ban (which each retry EXTENDS).

    A transient error from the probe call counts as an unknown reading, not a fatal — a
    socket blip must not abort a cycle on its own.
    """
    last: int | None = None
    for attempt in range(max_polls):
        try:
            client.fetch_time()
            last = used_weight(client)
        except Exception:  # noqa: BLE001 — a probe blip is an unknown reading, not a failure
            last = None
        if last is not None and last < threshold:
            return last
        if attempt < max_polls - 1:
            sleep(poll_seconds)
    raise HeadroomTimeout(
        f"no weight window under {threshold}/{WEIGHT_LIMIT_PER_MIN} after {max_polls} polls "
        f"(last reading: {last if last is not None else 'unknown'}). The IP's budget is being "
        f"consumed by a co-consumer; firing a heavy burst now would trip a fresh 418 ban and "
        f"each retry extends it. Surface and stop — the window clears on its own."
    )


def guard(settings, *, threshold: int = DEFAULT_THRESHOLD, echo: Callable[[str], None] = print):
    """Block until this IP has weight headroom, using a throwaway weight-1 client.

    Call this at the top of any CLI that is about to make heavy calls, BEFORE constructing
    the real exchange (`FuturesExchange.from_settings` runs `load_markets` on the way in, so
    guarding after it would be too late). `threshold <= 0` disables the guard.
    """
    if threshold <= 0:
        return None
    from futures_fund.exchange import build_ccxt

    w = wait_for_headroom(build_ccxt(settings), threshold=threshold)
    echo(f"[rate-limit] used weight {w}/{WEIGHT_LIMIT_PER_MIN} — headroom OK")
    return w

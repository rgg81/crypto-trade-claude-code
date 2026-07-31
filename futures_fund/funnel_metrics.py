"""Funnel conversion — measure the EDGE -> EXECUTION pipeline, not the cash balance.

WHY THIS EXISTS (cy313). The desk was visibly under-deployed and ~7.5% behind its 5%/mo line. The
tempting fix was a cash quota ("70% must be invested"). That would have been actively harmful: the
binding constraint was never willingness, it was geometry — on that day EVERY liquid candidate's
nearest structure sat 0.76-0.90 ATR from the mark, and a noise-legal 0.6-ATR stop caps a market
entry's structure-capped RR at `d/0.6` (so RR 2.0 needs d >= 1.2). A quota would have forced
RR ~1.0-1.3 entries in place of the RR 2.1-2.2 stop_entries the team correctly armed instead.

Counting the FUNNEL finds the real thing. The cy295-313 diagnosis was: ZERO market entries in 19
cycles, 8 arm-attempts against 53 edge-aligned declines = ~6% conversion — which located an actual
BLOCKAGE (the with-regime market-entry path was gated on an ADX bar essentially nothing printed,
a silent off-switch; lesson d6da6f70). A quota hides that; a conversion rate exposes it.

Two readings, deliberately separated:
  * `conversion` — over a trailing window, acted / (acted + edge-aligned declines). None when the
    desk identified NO edge at all, because a genuinely dry board is not a blockage and must not
    read as 0%.
  * `stalled_streak` — consecutive most-recent cycles that FOUND edge and acted on none. This is
    the blockage signature; a dry board never contributes to it.

Read-only: `state/cycle/*/report.json` (what the desk DID) and `memory/flat-decisions.jsonl` (what
it DECLINED, with the cy309-corrected `edge_aligned` semantics). Never raises — the panel is
advisory and must never break a cycle.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

# A cycle counts as "acted" if the desk armed a trigger or took a market entry. A resting trigger
# IS deployment even though it holds no cash yet — which is exactly why cash-based quotas mislead.
_ACTED_KEYS = ("triggers_armed", "market_entries")


@dataclass(frozen=True)
class FunnelStats:
    cycles: int = 0
    armed: int = 0
    market_entries: int = 0
    opened: int = 0
    edge_declined: int = 0
    acted: int = 0
    identified: int = 0
    conversion: float | None = None   # None = no edge identified in the window (not 0%)
    stalled_streak: int = 0           # consecutive recent cycles: found edge, acted on nothing


def _cycle_dirs(state_dir) -> list[int]:
    root = Path(state_dir) / "cycle"
    if not root.is_dir():
        return []
    out = []
    for p in root.iterdir():
        if p.is_dir() and p.name.isdigit():
            out.append(int(p.name))
    return sorted(out)


def _read_report(state_dir, n: int) -> dict:
    p = Path(state_dir) / "cycle" / str(n) / "report.json"
    try:
        d = json.loads(p.read_text())
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001 — a corrupt/absent report contributes nothing, never raises
        return {}


def _declines_by_cycle(memory_dir) -> dict[int, int]:
    """Edge-ALIGNED declines per cycle. Non-edge-aligned rows are excluded on purpose: passing on
    a setup that never matched the desk's edge is a correct decision, not a missed conversion."""
    p = Path(memory_dir) / "flat-decisions.jsonl"
    out: dict[int, int] = {}
    try:
        lines = p.read_text().splitlines()
    except Exception:  # noqa: BLE001
        return out
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except Exception:  # noqa: BLE001 — skip a half-written line, keep the rest
            continue
        if not isinstance(r, dict) or not r.get("edge_aligned"):
            continue
        c = r.get("cycle")
        if isinstance(c, int):
            out[c] = out.get(c, 0) + 1
    return out


def read_funnel_stats(state_dir, memory_dir, *, window: int = 12) -> FunnelStats:
    """Conversion + stall stats over the last `window` served cycles. Never raises."""
    try:
        cycles = _cycle_dirs(state_dir)[-window:] if window > 0 else _cycle_dirs(state_dir)
        if not cycles:
            return FunnelStats()
        declines = _declines_by_cycle(memory_dir)
        armed = market = opened = declined = 0
        per_cycle: list[tuple[int, int]] = []          # (acted, edge_declined) oldest -> newest
        for n in cycles:
            rep = _read_report(state_dir, n)
            def _num(key, _rep=rep):
                v = _rep.get(key)
                return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0
            a = _num("triggers_armed")
            m = _num("market_entries")
            armed += a
            market += m
            opened += _num("opened")
            d = declines.get(n, 0)
            declined += d
            per_cycle.append((a + m, d))
        acted = armed + market
        identified = acted + declined
        conversion = (acted / identified) if identified > 0 else None
        streak = 0
        for a, d in reversed(per_cycle):               # newest first
            if a == 0 and d > 0:
                streak += 1
            else:
                break
        return FunnelStats(cycles=len(cycles), armed=armed, market_entries=market, opened=opened,
                           edge_declined=declined, acted=acted, identified=identified,
                           conversion=conversion, stalled_streak=streak)
    except Exception:  # noqa: BLE001 — advisory panel; degrade to empty rather than break a cycle
        return FunnelStats()


def funnel_block(stats: FunnelStats, *, min_conversion: float = 0.25,
                 stall_alert: int = 3) -> str:
    """A ready-to-inject FUNNEL block for the agent prompts — the deployment counterpart to
    `pacing.performance_block`. It reports how much identified edge actually reached execution and
    raises an ALERT on the blockage signature, WITHOUT ever instructing anyone to take a trade:
    the remedy for a low conversion is to find the mechanical thing that is refusing setups, not
    to lower the bar (that distinction is the whole reason this metric exists rather than a cash
    quota). Pure/no-I/O."""
    if stats.identified <= 0:
        return ("FUNNEL (edge -> execution): no edge-aligned setups identified in the last "
                f"{stats.cycles} cycles — nothing to convert. A genuinely dry board is not a "
                "blockage; do not read this as 0%.")
    conv = stats.conversion or 0.0
    head = (f"FUNNEL (edge -> execution), last {stats.cycles} cycles: "
            f"{stats.acted}/{stats.identified} = {conv:.0%} converted "
            f"({stats.armed} armed, {stats.market_entries} market entries, {stats.opened} opened; "
            f"{stats.edge_declined} edge-aligned setups declined).")
    alert = ""
    if stats.stalled_streak >= stall_alert:
        alert = (f"\n⚠ ALERT: {stats.stalled_streak} consecutive cycles found edge and acted on "
                 "NONE. That is the blockage signature, not caution — look for the mechanical "
                 "rule that is refusing setups (a threshold nobody meets, an entry style the "
                 "geometry forbids, a leg the whole board lacks) and NAME it. Do not respond by "
                 "taking a worse trade.")
    elif conv < min_conversion:
        alert = (f"\n⚠ ALERT: conversion is below {min_conversion:.0%}. Identified edge is not "
                 "reaching execution — diagnose the mechanism before concluding the tape is dry.")
    return head + alert

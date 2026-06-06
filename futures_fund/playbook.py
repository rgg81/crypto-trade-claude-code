"""Regime-routed strategy playbook (Pillar 2 — ADAPT: win in ALL market conditions).

Maps a symbol's regime quadrant (from its brief's `regime` field) to the IN-SEASON strategies, so
the team switches playbook WITH the tape instead of running one playbook (positioning
flush-short/squeeze-long) everywhere regardless of regime. Pure/advisory: it shapes which setups the
analysts/RM/Trader hunt; the deterministic gate still owns all risk/sizing.

ALL-WEATHER doctrine: market-neutral here means PROFIT IN ALL CONDITIONS (trend, range, chop/
madness) — NOT holding ~zero net exposure. Net exposure is a managed risk parameter, not a forced
zero; a single regime-aligned position is valid.
"""
from __future__ import annotations

# quadrant -> (in-season strategies, one-line guidance)
_PLAYBOOK: dict[str, tuple[list[str], str]] = {
    "low_vol_trend": (
        ["trend-follow", "breakout", "pullback-continuation", "squeeze-long/flush-short"],
        "Clean trend: RIDE it — pullback/breakout entries WITH the trend, full size."),
    "high_vol_trend": (
        ["trend-follow", "breakdown/breakout trigger", "positioning flush-short/squeeze-long"],
        "Strong volatile trend: with-regime continuation; gate only the counter-trend knife."),
    "low_vol_range": (
        ["mean-reversion", "fade-range-edges", "relative-value"],
        "Quiet range: FADE band edges (short resistance / long support, RR>=2); relative-value."),
    "high_vol_range": (
        ["mean-reversion-small", "relative-value", "reduce-size"],
        "Choppy/madness: smaller size, fade extremes selectively, prefer relative-value."),
    "transition": (
        ["confirmation-only", "wait-for-break"],
        "Regime unclear: confirmation-gated entries only; no naked directional knife."),
}

_DEFAULT: tuple[list[str], str] = (
    ["confirmation-only"], "Unknown quadrant: confirmation-gated entries only.")


def playbook_for(quadrant: str) -> dict:
    """In-season strategies + guidance for a quadrant. Unknown -> confirmation-only default."""
    strategies, guidance = _PLAYBOOK.get(quadrant, _DEFAULT)
    return {"quadrant": quadrant, "strategies": list(strategies), "guidance": guidance}


def is_range(quadrant: str) -> bool:
    """True for range quadrants where MEAN-REVERSION (not trend-follow) is the in-season edge."""
    return quadrant in ("low_vol_range", "high_vol_range")

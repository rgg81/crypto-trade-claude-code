from __future__ import annotations

from futures_fund.config import Settings


def live_allowed(settings: Settings, scorecard: dict) -> bool:
    """Live trading is permitted ONLY when explicitly enabled AND the desk has graduated.
    (The cycle additionally checks the HALT flag.) Survival-first: default-deny."""
    if not getattr(settings, "live", False):
        return False
    g = scorecard.get("graduation")
    return isinstance(g, dict) and g.get("status") == "graduated"

"""Anti-hallucination proposal audit (Pillar 4 — AUDIT).

The review found the risk gate trusts agent-supplied numbers (entry/atr) without cross-checking
them against the brief's COMPUTED ground truth — so a fabricated entry (a fantasy paper fill at a
price that never traded) or a fabricated atr (wrong-symbol/units sizing) could reach execution.
This adds a deterministic cross-validation BEFORE the protected gate: a proposal whose entry
diverges too far from the brief it was handed, or whose atr is wildly off the brief's computed atr,
is DROPPED fail-loud. It ADDS a check; it weakens nothing and never touches a protected module.

FAIL-OPEN on MISSING ground truth (no mark/atr to check against) so the audit catches clear
fabrications WITHOUT becoming a new deploy-blocker that fights the 5%/mo pursuit — the gate's own
limits still apply. Symmetric: identical treatment for longs and shorts.
"""
from __future__ import annotations

ENTRY_TOL_MARKET = 0.05    # a MARKET open's entry must be within 5% of the brief's last_close
TRIGGER_TOL = 0.25         # a breakout/breakdown TRIGGER level may sit further (<=25%) from mark
ATR_LO, ATR_HI = 0.4, 2.5  # agent atr must be within [0.4x, 2.5x] the brief's computed atr


def _finite_pos(x) -> bool:
    try:
        return x is not None and float(x) > 0 and float(x) == float(x)  # excludes None/<=0/NaN
    except (TypeError, ValueError):
        return False


def audit_entry(entry, mark, *, is_trigger: bool) -> tuple[bool, str]:
    """Entry must be near the brief's last_close it was derived from. Triggers (breakout/breakdown/
    pullback levels) get a wider band than market opens. FAIL-OPEN if mark/entry missing."""
    if not _finite_pos(mark) or not _finite_pos(entry):
        return True, ""
    tol = TRIGGER_TOL if is_trigger else ENTRY_TOL_MARKET
    dev = abs(float(entry) - float(mark)) / float(mark)
    if dev > tol:
        kind = "trigger" if is_trigger else "market"
        return False, (f"{kind} entry {entry} deviates {dev:.0%} from brief mark {mark} "
                       f"(> {tol:.0%})")
    return True, ""


def audit_atr(atr, brief_atr) -> tuple[bool, str]:
    """Agent atr must be in-family with the brief's computed atr (catches a fabricated /
    wrong-symbol / wrong-units atr that would mis-size the trade). FAIL-OPEN if either missing."""
    if not _finite_pos(atr) or not _finite_pos(brief_atr):
        return True, ""
    ratio = float(atr) / float(brief_atr)
    if ratio < ATR_LO or ratio > ATR_HI:
        return False, (f"atr {atr} is {ratio:.1f}x the brief's computed {brief_atr} "
                       f"(outside [{ATR_LO},{ATR_HI}])")
    return True, ""


def audit_item(item: dict, ground_truth: dict, *, is_trigger: bool) -> tuple[bool, str]:
    """Cross-check one proposal/trigger dict vs `ground_truth` = {<raw symbol>: {"mark","atr"}}.
    Returns (ok, reason). A symbol with no ground-truth entry is FAIL-OPEN (kept)."""
    gt = (ground_truth or {}).get(item.get("symbol"))
    if not gt:
        return True, ""
    entry = item.get("entry", item.get("trigger_level"))
    ok, reason = audit_entry(entry, gt.get("mark"), is_trigger=is_trigger)
    if not ok:
        return False, reason
    return audit_atr(item.get("atr"), gt.get("atr"))


def audit_batch(items: list[dict], ground_truth: dict, *, is_trigger: bool) -> tuple[list, list]:
    """Partition (kept, dropped). `dropped` items carry a `_audit_reason` for the report/log."""
    kept, dropped = [], []
    for it in items or []:
        ok, reason = audit_item(it, ground_truth, is_trigger=is_trigger)
        if ok:
            kept.append(it)
        else:
            dropped.append({**it, "_audit_reason": reason})
    return kept, dropped

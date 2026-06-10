from __future__ import annotations

import json
import math
from pathlib import Path

QUADRANTS = ("high_vol_trend", "low_vol_trend", "high_vol_range", "low_vol_range")
SEED = 2.0
BAND = (1.6, 2.5)            # (hard floor, ceiling) — the adaptive RR floor never leaves this band
LOOSEN_STEP = 0.05          # slow to relax the limit
TIGHTEN_STEP = 0.10         # fast to re-tighten (safety-asymmetric)
N_MIN = 8                   # min decided (won+lost) samples before a quadrant floor moves
WIN_HI = 0.60               # would-have-won rate above this -> vetoes cost winners -> loosen
WIN_LO = 0.40               # below this -> vetoes save losers -> tighten


def _path(state_dir) -> Path:
    return Path(state_dir) / "rr_floor.json"


def clamp(x: float) -> float:
    lo, hi = BAND
    return max(lo, min(hi, x))


def load_rr_floor(state_dir) -> dict:
    """All four quadrant floors + updated_cycle. FAIL-SAFE: missing file / corrupt JSON / missing
    key / non-finite value -> SEED (today's 2.0 behaviour), never raises."""
    out: dict = {q: SEED for q in QUADRANTS}
    out["updated_cycle"] = 0
    try:
        raw = json.loads(_path(state_dir).read_text())
    except (OSError, ValueError):
        return out
    if not isinstance(raw, dict):
        return out
    for q in QUADRANTS:
        v = raw.get(q)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v):
            out[q] = float(v)
    uc = raw.get("updated_cycle")
    if isinstance(uc, int) and not isinstance(uc, bool):
        out["updated_cycle"] = uc
    return out


def save_rr_floor(state_dir, state: dict) -> None:
    p = _path(state_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, indent=2))


def effective_rr_floor(quadrant: str, state: dict) -> float:
    """The floor a trade in `quadrant` is judged on, clamped to BAND. Unknown quadrant / non-finite
    stored value -> SEED (then clamped). Pure."""
    raw = state.get(quadrant, SEED)
    if not isinstance(raw, (int, float)) or isinstance(raw, bool) or not math.isfinite(raw):
        raw = SEED
    return clamp(float(raw))


def adapt_rr_floor(state: dict, won_lost_by_quadrant: dict, cycle_no: int) -> tuple[dict, list]:
    """Nudge each quadrant's floor from its trailing-window (won, lost) tally. Safety-asymmetric:
    w>WIN_HI (vetoes cost winners) -> loosen by LOOSEN_STEP; w<WIN_LO (vetoes save losers) ->
    tighten by TIGHTEN_STEP; dead-band between = no change. Needs >= N_MIN decided samples. Pure;
    returns (new_state, [human-readable change strings]). Always clamps to BAND."""
    new = dict(state)
    changes: list = []
    for q in QUADRANTS:
        won, lost = won_lost_by_quadrant.get(q, (0, 0))
        decided = won + lost
        if decided < N_MIN:
            continue
        w = won / decided
        cur = clamp(float(state.get(q, SEED)))
        if w > WIN_HI:
            nxt = clamp(cur - LOOSEN_STEP)
        elif w < WIN_LO:
            nxt = clamp(cur + TIGHTEN_STEP)
        else:
            continue
        if nxt != cur:
            new[q] = nxt
            verb = "cost" if w > WIN_HI else "saved"
            changes.append(f"rr_floor {q} {cur:.2f}->{nxt:.2f} "
                           f"(vetoes {verb} {won}/{decided}, w={w:.2f})")
    if changes:
        new["updated_cycle"] = cycle_no
    return new, changes

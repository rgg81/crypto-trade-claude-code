from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path


def _path(state_dir) -> Path:
    return Path(state_dir) / "shadow-ledger.jsonl"


def record_shadow(state_dir, ts: datetime, cycle: int, entries: list[dict]) -> int:
    """Record proposals the risk gate VETOED (at zero capital) so we can later measure whether
    the veto saved or cost us — the value of the risk filter (spec §9)."""
    p = _path(state_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as f:
        for e in entries:
            f.write(json.dumps({**e, "ts": ts.isoformat(), "cycle": cycle}) + "\n")
    return len(entries)


def shadow_ledger(state_dir) -> list[dict]:
    p = _path(state_dir)
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


def shadow_outcome(entry: dict, bar_high: float, bar_low: float) -> dict:
    """Hypothetical outcome of a vetoed trade over one bar (R-multiple if stop/tp touched).
    `veto_saved` is True when the would-be trade would have lost (so vetoing it was correct)."""
    e, stop = entry["entry"], entry["stop"]
    tp = entry["take_profits"][0] if entry.get("take_profits") else None
    risk = abs(e - stop)
    hit, level = None, None
    if entry["direction"] == "long":
        if bar_low <= stop:
            hit, level = "stop", stop
        elif tp is not None and bar_high >= tp:
            hit, level = "take_profit", tp
    else:
        if bar_high >= stop:
            hit, level = "stop", stop
        elif tp is not None and bar_low <= tp:
            hit, level = "take_profit", tp
    if hit is None:
        return {"hit": None, "r_multiple": 0.0, "veto_saved": False}
    gain = (level - e) if entry["direction"] == "long" else (e - level)
    r = gain / risk if risk > 0 else 0.0
    return {"hit": hit, "r_multiple": r, "veto_saved": r < 0}


HORIZON = 12   # bars (~2 days at 4h) a vetoed trade is tracked before it 'expires' undecided


def score_shadow_first_touch(entry: dict, bars: list[dict]) -> str:
    """First-touch outcome of a would-be (vetoed) trade over up to HORIZON bars AFTER the veto.
    `bars` is chronological dicts with 'high'/'low'. Returns 'won' (TP touched before stop), 'lost'
    (stop touched first; a same-bar TP+stop ambiguity resolves conservatively to 'lost'), 'pending'
    (< HORIZON bars seen, neither touched), or 'expired' (>= HORIZON bars, neither touched)."""
    stop = entry["stop"]
    tps = entry.get("take_profits") or []
    tp = tps[0] if tps else None
    is_long = entry["direction"] == "long"
    for bar in bars[:HORIZON]:
        hi, lo = bar.get("high"), bar.get("low")
        if hi is None or lo is None:
            continue
        if is_long:
            stop_hit, tp_hit = lo <= stop, (tp is not None and hi >= tp)
        else:
            stop_hit, tp_hit = hi >= stop, (tp is not None and lo <= tp)
        if stop_hit:                      # conservative: stop wins a same-bar tie
            return "lost"
        if tp_hit:
            return "won"
    return "expired" if len(bars) >= HORIZON else "pending"


def tally_resolutions(scored: dict, trail_w: int) -> dict:
    """Per-quadrant (won, lost) counts over the most recent `trail_w` DECIDED (won/lost) entries.
    `scored` maps id -> {outcome, quadrant, ...}; 'pending'/'expired' are excluded. Insertion order
    of `scored` is the chronological order (entries are appended as they resolve)."""
    by_q: dict = {}
    decided = [v for v in scored.values() if v.get("outcome") in ("won", "lost")]
    for v in decided[-trail_w:]:
        q = v.get("quadrant")
        if q is None:
            continue
        won, lost = by_q.get(q, (0, 0))
        by_q[q] = (won + (v["outcome"] == "won"), lost + (v["outcome"] == "lost"))
    return by_q


def _scored_path(state_dir) -> Path:
    return Path(state_dir) / "shadow-scored.json"


def load_scored(state_dir) -> dict:
    try:
        out = json.loads(_scored_path(state_dir).read_text())
        return out if isinstance(out, dict) else {}
    except (OSError, ValueError):
        return {}


def save_scored(state_dir, scored: dict) -> None:
    p = _scored_path(state_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(scored))

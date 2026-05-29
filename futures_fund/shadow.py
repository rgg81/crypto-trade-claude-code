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

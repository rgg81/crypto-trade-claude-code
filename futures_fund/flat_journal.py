"""Journal of FLAT / declined-setup verdicts.

The desk only ever journaled OPENED trades, so reflection could only mine a winners-vs-losers
contrast — structurally producing risk-reducing ('don't') lessons only. To learn whether standing
aside HELPS or COSTS, we must also persist the trades the desk DECLINED, flagged by whether they
matched its proven edge, then later evaluate how price actually moved. That closes the feedback
loop so the corpus can mint enabling ('DO take it when X') lessons too.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path


def _store(memory_dir) -> Path:
    return Path(memory_dir) / "flat-decisions.jsonl"


def append_flat_decision(memory_dir, fields: dict, ts: datetime) -> str:
    """Record a FLAT verdict. Expected fields: cycle, symbol, regime, rating, reason,
    edge_aligned (bool — did it match the crowded-short squeeze-long edge?), favored_side
    ('long'|'short' — the direction the passed-on setup leaned), mark (price at decision).
    Outcome fields (evaluated, favored_move_pct, flat_cost_us) are patched later."""
    data = {**fields, "ts": ts.isoformat() if hasattr(ts, "isoformat") else ts}
    data.setdefault("id", uuid.uuid4().hex)
    data.setdefault("evaluated", False)
    p = _store(memory_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as fh:
        fh.write(json.dumps(data, default=str) + "\n")
    return data["id"]


def read_flat_decisions(memory_dir) -> list[dict]:
    p = _store(memory_dir)
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


def _write_all(memory_dir, rows: list[dict]) -> None:
    _store(memory_dir).write_text("".join(json.dumps(r, default=str) + "\n" for r in rows))


def patch_flat_outcome(memory_dir, fid: str, fields: dict) -> bool:
    rows = read_flat_decisions(memory_dir)
    hit = False
    for r in rows:
        if r.get("id") == fid:
            r.update(fields)
            hit = True
    if hit:
        _write_all(memory_dir, rows)
    return hit


def evaluate_pending_flats(memory_dir, marks: dict[str, float], now: datetime,
                           *, min_move: float = 0.02) -> int:
    """For each un-evaluated, edge-aligned FLAT, compare the decision mark to the current mark in
    the setup's FAVORED direction. `flat_cost_us` is True when price moved >= min_move our way
    (standing aside cost us) and False when it moved against (standing aside was right). Only
    edge-aligned flats are evaluated (those are the ones whose suppression is the bug). Returns the
    number newly evaluated."""
    rows = read_flat_decisions(memory_dir)
    n = 0
    for r in rows:
        if r.get("evaluated") or not r.get("edge_aligned"):
            continue
        m0, sym = r.get("mark"), r.get("symbol")
        m1 = marks.get(sym)
        if not m0 or not m1:
            continue
        side = r.get("favored_side", "long")
        move = (m1 - m0) / m0 * (1.0 if side == "long" else -1.0)
        r.update({"evaluated": True, "eval_mark": m1,
                  "eval_ts": now.isoformat() if hasattr(now, "isoformat") else now,
                  "favored_move_pct": move, "flat_cost_us": move >= min_move})
        n += 1
    if n:
        _write_all(memory_dir, rows)
    return n

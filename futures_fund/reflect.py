from __future__ import annotations

from datetime import datetime
from pathlib import Path

from futures_fund.journal import read_all_decisions
from futures_fund.lessons import append_lesson


def reflection_payload(memory_dir) -> dict:
    """Split closed decisions into winners/losers for the Reflector subagent to contrast.
    (The Reflector reasons over this; promotion/validation gating is Phase C.)"""
    closed = [d for d in read_all_decisions(memory_dir) if d.get("realized_pnl") is not None]
    winners = [d for d in closed if d["realized_pnl"] > 0]
    losers = [d for d in closed if d["realized_pnl"] <= 0]
    return {"winners": winners, "losers": losers, "n_closed": len(closed)}


def record_lesson(memory_dir, text: str, regime: str | None, tags: list[str],
                  importance: int, provenance: list[str], ts: datetime) -> str:
    """Persist a Reflector-produced lesson as a CANDIDATE (structured store + human lessons.md)."""
    lid = append_lesson(memory_dir, {
        "text": text, "regime": regime, "tags": tags, "importance": importance,
        "provenance": provenance, "state": "candidate",
    }, ts=ts)
    md = Path(memory_dir) / "lessons" / "lessons.md"
    md.parent.mkdir(parents=True, exist_ok=True)
    with md.open("a") as fh:
        fh.write(f"\n- [CANDIDATE {ts:%Y-%m-%d}] ({regime or 'any'}) {text} "
                 f"<tags: {', '.join(tags)}; from: {', '.join(provenance)}>\n")
    return lid

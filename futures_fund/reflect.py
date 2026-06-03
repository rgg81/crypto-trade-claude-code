from __future__ import annotations

from datetime import datetime
from pathlib import Path

from futures_fund.flat_journal import read_flat_decisions
from futures_fund.journal import read_all_decisions
from futures_fund.lessons import append_lesson

_WINNER_THRESHOLD = 0.0  # strict: only strictly-positive realized PnL is a win (breakeven = loss)


def reflection_payload(memory_dir) -> dict:
    """Contrast material for the Reflector: winners vs losers (which closed trades worked), AND
    declined edge-aligned setups (which FLATs the desk passed on) so reflection can mint ENABLING
    'DO take it when X' lessons — not only loss-avoidance rules. `missed_opportunities` are the
    edge-aligned flats that, on evaluation, moved our way (standing aside cost the desk)."""
    closed = [d for d in read_all_decisions(memory_dir) if d.get("realized_pnl") is not None]
    winners = [d for d in closed if d["realized_pnl"] > _WINNER_THRESHOLD]
    losers = [d for d in closed if d["realized_pnl"] <= _WINNER_THRESHOLD]
    flats = read_flat_decisions(memory_dir)
    declined = [f for f in flats if f.get("edge_aligned")]
    missed = [f for f in declined if f.get("evaluated") and f.get("flat_cost_us")]
    return {"winners": winners, "losers": losers, "n_closed": len(closed),
            "declined_edge_setups": declined, "missed_opportunities": missed}


def record_lesson(memory_dir, text: str, regime: str | None, tags: list[str],
                  importance: int, provenance: list[str], ts: datetime,
                  polarity: str = "restrictive") -> str:
    """Persist a Reflector-produced lesson as a CANDIDATE (structured store + human lessons.md).
    `polarity` (restrictive|enabling|process) keeps the corpus two-sided — the Reflector must be
    able to record DO-rules, not only prohibitions."""
    lid = append_lesson(memory_dir, {
        "text": text, "regime": regime, "tags": tags, "importance": importance,
        "provenance": provenance, "state": "candidate", "polarity": polarity,
    }, ts=ts)
    md = Path(memory_dir) / "lessons" / "lessons.md"
    md.parent.mkdir(parents=True, exist_ok=True)
    with md.open("a") as fh:
        fh.write(f"\n- [CANDIDATE {ts:%Y-%m-%d}] ({regime or 'any'}/{polarity}) {text} "
                 f"<tags: {', '.join(tags)}; from: {', '.join(provenance)}>\n")
    return lid


def record_lessons(memory_dir, lessons: list[dict], ts: datetime) -> list[str]:
    """Deterministically persist a Reflector's lesson LIST (e.g. state/cycle/N/lessons.json) to the
    corpus, so the reflect phase ALWAYS appends — never depending on the LLM Reflector agent to
    remember to call record_lesson (which it did in cycle 22 but not cycle 23). Idempotent by exact
    text (RETRY-safe): a lesson already in the corpus is skipped; blank text is skipped; missing
    fields take record_lesson's defaults. Returns the ids of the lessons actually appended."""
    from futures_fund.lessons import read_lessons
    existing = {lz.text for lz in read_lessons(memory_dir)}
    ids: list[str] = []
    for lesson in lessons:
        text = str(lesson.get("text") or "").strip()
        if not text or text in existing:
            continue
        ids.append(record_lesson(
            memory_dir, text=text, regime=lesson.get("regime"), tags=list(lesson.get("tags") or []),
            importance=int(lesson.get("importance", 5)),
            provenance=list(lesson.get("provenance") or []), ts=ts,
            polarity=str(lesson.get("polarity", "restrictive"))))
        existing.add(text)
    return ids

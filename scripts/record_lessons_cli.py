"""Deterministically persist the Reflector's lessons to the corpus AND apply its lesson-confirmation
scoring (the reflect phase must ALWAYS do both — not rely on the LLM Reflector agent to remember,
nor on the orchestrator hand-running promote_lesson_cli). Reads the Reflector's
`state/cycle/N/lessons.json`: appends each `lessons` entry via record_lessons (idempotent by text),
and applies the `lesson_scoring` block ({confirm:[{id,why}], demote:[{id,why}]} — which RETRIEVED
lessons a CLOSED trade's outcome validated/refuted) via the DSR-gated apply_lesson_scoring (#255).

    uv run python scripts/record_lessons_cli.py --cycle N
"""
from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime

from futures_fund.cycle_io import load_output
from futures_fund.lessons import apply_lesson_scoring
from futures_fund.reflect import record_lessons
from futures_fund.scorecard import build_scorecard


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cycle", type=int, required=True)
    args = ap.parse_args()
    payload = load_output("state", args.cycle, "lessons") or {}
    lessons = payload.get("lessons", []) if isinstance(payload, dict) else (payload or [])
    ids = record_lessons("memory", lessons, ts=datetime.now(UTC))
    scoring = payload.get("lesson_scoring") if isinstance(payload, dict) else None
    dsr = build_scorecard("state", "memory").get("dsr_pvalue", 0.0)
    applied = apply_lesson_scoring("memory", scoring, dsr_pvalue=dsr,
                                   cycle_no=args.cycle)
    print(json.dumps({"cycle": args.cycle, "appended": len(ids), "lesson_ids": ids,
                      "lesson_scoring": applied, "dsr_pvalue": dsr}, default=str))


if __name__ == "__main__":
    main()

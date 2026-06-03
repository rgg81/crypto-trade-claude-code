"""Deterministically persist the Reflector's lessons to the corpus (the reflect phase must ALWAYS
append — not rely on the LLM Reflector agent to remember). Reads the Reflector's
`state/cycle/N/lessons.json` and appends each lesson via record_lessons (idempotent by text).

    uv run python scripts/record_lessons_cli.py --cycle N
"""
from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime

from futures_fund.cycle_io import load_output
from futures_fund.reflect import record_lessons


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cycle", type=int, required=True)
    args = ap.parse_args()
    payload = load_output("state", args.cycle, "lessons") or {}
    lessons = payload.get("lessons", []) if isinstance(payload, dict) else (payload or [])
    ids = record_lessons("memory", lessons, ts=datetime.now(UTC))
    print(json.dumps({"cycle": args.cycle, "appended": len(ids), "lesson_ids": ids}, default=str))


if __name__ == "__main__":
    main()

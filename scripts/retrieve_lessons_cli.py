"""Retrieve regime-relevant lessons for the debate/trader prompts.

    uv run python scripts/retrieve_lessons_cli.py --cycle N --regime high_vol_trend \
--tags trend,funding --k 5
"""
from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime

from futures_fund.cycle_io import save_output
from futures_fund.orchestration import lessons_step


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cycle", type=int, required=True)
    ap.add_argument("--regime", default=None,
                    help="query context(s), comma-separated: pass BOTH the symbol quadrant AND the "
                         "desk engine label, e.g. 'high_vol_trend,risk_off' (cy78 fix)")
    ap.add_argument("--tags", default="")
    ap.add_argument("--k", type=int, default=5)
    args = ap.parse_args()
    tags = [t for t in args.tags.split(",") if t]
    # accept comma-separated contexts; a single value stays a plain string (back-compat)
    ctx = [r for r in (args.regime or "").split(",") if r] or None
    regime = ctx[0] if ctx and len(ctx) == 1 else ctx
    lessons = lessons_step("memory", datetime.now(UTC), regime, tags, args.k)
    save_output("state", args.cycle, "lessons", {"lessons": lessons})
    print(json.dumps({"lessons": lessons}, indent=2, default=str))


if __name__ == "__main__":
    main()

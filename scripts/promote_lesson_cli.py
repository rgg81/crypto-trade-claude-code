"""Apply a Reflector-decided lesson state change. `confirm` is DSR-gated: a lesson only
promotes to VALIDATED when the desk's edge is statistically proven.

    uv run python scripts/promote_lesson_cli.py --id <lesson_id> --action confirm|demote|retire
"""
from __future__ import annotations

import argparse

from futures_fund.lessons import demote_lesson, retire_lesson, statistically_promote
from futures_fund.scorecard import build_scorecard


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", required=True)
    ap.add_argument("--action", choices=["confirm", "demote", "retire"], required=True)
    args = ap.parse_args()
    if args.action == "confirm":
        dsr = build_scorecard("state", "memory").get("dsr_pvalue", 0.0)
        ok = statistically_promote("memory", args.id, dsr_pvalue=dsr)
    else:
        ok = {"demote": demote_lesson, "retire": retire_lesson}[args.action]("memory", args.id)
    print(f"{args.action} {args.id}: {'ok' if ok else 'not found'}")


if __name__ == "__main__":
    main()

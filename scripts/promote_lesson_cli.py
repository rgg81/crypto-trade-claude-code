"""Apply a Reflector-decided lesson state change.

    uv run python scripts/promote_lesson_cli.py --id <lesson_id> --action confirm|demote|retire
"""
from __future__ import annotations

import argparse

from futures_fund.lessons import confirm_lesson, demote_lesson, retire_lesson


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", required=True)
    ap.add_argument("--action", choices=["confirm", "demote", "retire"], required=True)
    args = ap.parse_args()
    fn = {"confirm": confirm_lesson, "demote": demote_lesson, "retire": retire_lesson}[args.action]
    ok = fn("memory", args.id)
    print(f"{args.action} {args.id}: {'ok' if ok else 'not found'}")


if __name__ == "__main__":
    main()

"""Funnel conversion panel — how much IDENTIFIED edge actually reached execution.

The deployment counterpart to the pacing/performance block. Answers "is the desk under-deployed
because the tape is dry, or because something mechanical is refusing setups?" — the question a
cash-deployment quota cannot answer and would paper over (see futures_fund.funnel_metrics).

    uv run python scripts/funnel_cli.py                 # default 12-cycle window
    uv run python scripts/funnel_cli.py --window 20
    uv run python scripts/funnel_cli.py --json          # machine-readable

Read-only. Makes ZERO exchange calls and ZERO writes.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict

from futures_fund.funnel_metrics import funnel_block, read_funnel_stats


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--state-dir", default="state")
    ap.add_argument("--memory-dir", default="memory")
    ap.add_argument("--window", type=int, default=12, help="trailing cycles to sample")
    ap.add_argument("--json", action="store_true", help="emit the raw stats as JSON")
    args = ap.parse_args(argv)
    stats = read_funnel_stats(args.state_dir, args.memory_dir, window=args.window)
    if args.json:
        print(json.dumps(asdict(stats), indent=1))
    else:
        print(funnel_block(stats))
    return 0


if __name__ == "__main__":
    sys.exit(main())

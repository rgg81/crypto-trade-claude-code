"""Phase 4.5 CLI: read analyst reports, write the screened top-N symbols.

    uv run python scripts/screen_cli.py --cycle N --top 5
"""
from __future__ import annotations

import argparse
import json

from futures_fund.cycle_io import load_output, save_output
from futures_fund.orchestration import screen_step


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cycle", type=int, required=True)
    ap.add_argument("--top", type=int, default=5)
    args = ap.parse_args()
    reports = load_output("state", args.cycle, "analyst_reports")
    symbols = screen_step(reports, top_n=args.top)
    save_output("state", args.cycle, "screened", {"symbols": symbols})
    print(json.dumps({"symbols": symbols}, indent=2))


if __name__ == "__main__":
    main()

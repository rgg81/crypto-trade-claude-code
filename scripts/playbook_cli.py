"""Playbook scorecard CLI (Learning Direction A) — prints the desk's realized-track-record advisory
for the Research Manager prompt. READ-ONLY; touches no protected module, writes no state.

    uv run python scripts/playbook_cli.py            # the RM advisory string
    uv run python scripts/playbook_cli.py --json     # the raw aggregation (for inspection)
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from futures_fund.journal import read_all_decisions
from futures_fund.playbook_scorecard import (
    aggregate_playbook,
    format_playbook_advisory,
    load_regime_by_cycle,
    playbook_advisory,
)


def _book_flat(state_dir) -> bool:
    """True if the desk currently holds NO open positions (cautions self-silence when flat)."""
    p = Path(state_dir) / "positions.json"
    try:
        return len(json.loads(p.read_text())) == 0
    except (OSError, ValueError, TypeError):
        return True


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--memory", default="memory")
    ap.add_argument("--state", default="state")
    ap.add_argument("--json", action="store_true", help="raw aggregation, not the advisory")
    args = ap.parse_args()
    if args.json:
        agg = aggregate_playbook(read_all_decisions(args.memory), load_regime_by_cycle(args.state))
        print(json.dumps(agg, indent=2))
        # also show the rendered advisory so the operator sees exactly what the RM will read
        print("\n--- advisory ---")
        print(format_playbook_advisory(agg, book_flat=_book_flat(args.state),
                                       total_closed=agg["coverage"]["n_closed"]))
    else:
        print(playbook_advisory(args.memory, args.state, book_flat=_book_flat(args.state)))


if __name__ == "__main__":
    main()

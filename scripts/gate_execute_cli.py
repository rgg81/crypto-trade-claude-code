"""Phases 7-10 CLI: gate + consolidate + execute the trader proposals; persist + report.

    uv run python scripts/gate_execute_cli.py --cycle N
"""
from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime

from futures_fund.config import load_settings
from futures_fund.cycle_io import load_output, save_output
from futures_fund.exchange import FuturesExchange
from futures_fund.orchestration import gate_execute_step


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cycle", type=int, required=True)
    args = ap.parse_args()
    settings = load_settings()
    ex = FuturesExchange.from_settings(settings)
    proposals = load_output("state", args.cycle, "proposals")["proposals"]
    report = gate_execute_step(ex, settings, "state", "memory",
                               now=datetime.now(UTC), cycle_no=args.cycle,
                               proposals=proposals)
    save_output("state", args.cycle, "report", report)
    print(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    main()

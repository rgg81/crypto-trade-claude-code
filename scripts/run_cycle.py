"""Run one paper trading cycle from the command line (deterministic baseline, no LLM):

    uv run python scripts/run_cycle.py --cycle 1

Uses config.yaml + env (testnet keys optional for public data). State in state/, memory in memory/.
"""
from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime

from futures_fund.config import load_settings
from futures_fund.cycle import run_cycle
from futures_fund.exchange import FuturesExchange


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one paper trading cycle")
    parser.add_argument("--cycle", type=int, default=1)
    args = parser.parse_args()
    settings = load_settings()
    exchange = FuturesExchange.from_settings(settings)
    report = run_cycle(exchange, settings, "state", "memory",
                       now=datetime.now(UTC), cycle_no=args.cycle)
    print(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    main()

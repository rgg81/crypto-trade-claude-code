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
    ap.add_argument("--symbols", default=None,
                    help="comma-separated unified symbols (the Watcher's picks); overrides "
                         "config. Held positions are folded in automatically.")
    args = ap.parse_args()
    settings = load_settings()
    # explicit --symbols (even empty) is the Watcher's universe for this cycle; never the default
    if args.symbols is not None:
        syms = [s.strip() for s in args.symbols.split(",") if s.strip()]
        settings = settings.model_copy(update={"symbols": syms})
    ex = FuturesExchange.from_settings(settings)
    payload = load_output("state", args.cycle, "proposals")
    report = gate_execute_step(ex, settings, "state", "memory",
                               now=datetime.now(UTC), cycle_no=args.cycle,
                               proposals=payload.get("proposals", []),
                               management=payload.get("management"))
    save_output("state", args.cycle, "report", report)
    print(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    main()

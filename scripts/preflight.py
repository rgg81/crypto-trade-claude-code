"""Phase 0-2 CLI: emit the per-cycle context (briefs/health) for the Watcher + analysts.

    uv run python scripts/preflight.py --cycle N
"""
from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime

from futures_fund.config import load_settings
from futures_fund.cycle_io import save_output
from futures_fund.exchange import FuturesExchange
from futures_fund.orchestration import preflight_step


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cycle", type=int, required=True)
    ap.add_argument("--symbols", default=None,
                    help="comma-separated unified symbols (the Watcher's picks); overrides "
                         "config. Held positions are folded into the universe automatically.")
    args = ap.parse_args()
    settings = load_settings()
    # explicit --symbols (even empty) is the Watcher's universe for this cycle; never the default
    if args.symbols is not None:
        syms = [s.strip() for s in args.symbols.split(",") if s.strip()]
        settings = settings.model_copy(update={"symbols": syms})
    ex = FuturesExchange.from_settings(settings)
    ctx = preflight_step(ex, settings, "state", "memory",
                         now=datetime.now(UTC), cycle_no=args.cycle)
    save_output("state", args.cycle, "context", ctx)
    print(json.dumps(ctx, indent=2, default=str))


if __name__ == "__main__":
    main()

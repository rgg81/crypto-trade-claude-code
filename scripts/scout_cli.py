"""Phase 2.5 CLI: scan the LIVE USD-M perp universe (top by 24h quote volume) for the Watcher.
Recomputed every cycle so the universe rotates with the market. Public/keyless.

    uv run python scripts/scout_cli.py --cycle N --top 30
"""
from __future__ import annotations

import argparse
import json

from futures_fund.config import load_settings
from futures_fund.cycle_io import save_output
from futures_fund.exchange import build_ccxt
from futures_fund.market_data import scan_universe


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cycle", type=int, required=True)
    ap.add_argument("--top", type=int, default=30)
    args = ap.parse_args()
    settings = load_settings()
    client = build_ccxt(settings)
    client.load_markets()
    universe = scan_universe(client, top_n=args.top)
    save_output("state", args.cycle, "universe", {"universe": universe})
    print(json.dumps({"universe": universe}, indent=2))


if __name__ == "__main__":
    main()

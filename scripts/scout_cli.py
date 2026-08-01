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
from futures_fund.rate_limit import DEFAULT_THRESHOLD, wait_for_headroom


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cycle", type=int, required=True)
    ap.add_argument("--top", type=int, default=30)
    ap.add_argument("--weight-threshold", type=int, default=DEFAULT_THRESHOLD,
                    help="launch only when the IP's used weight is under this (0 disables)")
    args = ap.parse_args()
    settings = load_settings()
    client = build_ccxt(settings)
    # cy317: a weight-1 probe proves only that no ban is ACTIVE — it is blind to a drained
    # weight budget, which is what makes the next heavy burst trip a FRESH ban. Wait for
    # real headroom before load_markets + fetch_tickers. Fail-loud if none appears.
    if args.weight_threshold > 0:
        w = wait_for_headroom(client, threshold=args.weight_threshold)
        print(f"[rate-limit] used weight {w} — headroom OK, launching scout", flush=True)
    client.load_markets()
    universe = scan_universe(client, top_n=args.top)
    save_output("state", args.cycle, "universe", {"universe": universe})
    print(json.dumps({"universe": universe}, indent=2))


if __name__ == "__main__":
    main()

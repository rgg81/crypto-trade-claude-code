"""Candidate price cards CLI (cy78 anti-hallucination backlog) — print the REAL deterministic levels
for a cycle's screened symbols so the orchestrator pastes ground truth (price/ATR/swings/DMI/RSI/
funding direction) into the debate/RM/Trader prompts instead of letting an agent guess a price.

    uv run python scripts/price_cards_cli.py --cycle N                       # all briefs
    uv run python scripts/price_cards_cli.py --cycle N --symbols "XMRUSDT,..."# screened subset
    uv run python scripts/price_cards_cli.py --cycle N --json                # raw JSON

Reads state/cycle/N/context.json (and screen.json for the default symbol set); READ-ONLY, no state.
"""
from __future__ import annotations

import argparse
import json

from futures_fund.cycle_io import load_output
from futures_fund.price_card import price_cards


def _fmt(v, nd=4):
    if isinstance(v, float):
        return f"{v:.{nd}g}"
    return "—" if v is None else str(v)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cycle", type=int, required=True)
    ap.add_argument("--symbols", default="", help="comma-separated symbols (raw or unified); all")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    context = load_output("state", args.cycle, "context")
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()] or None
    cards = price_cards(context, symbols=symbols)
    if args.json:
        print(json.dumps(cards, indent=2, default=str))
        return
    for c in cards:
        print(f"{c['symbol']}: last_close {_fmt(c['last_close'])} mark {_fmt(c['mark_price'])} "
              f"atr {_fmt(c['atr'])} | swingH {_fmt(c['swing_high'])} "
              f"swingL {_fmt(c['swing_low'])} | ADX {_fmt(c['adx'],3)} "
              f"+DI {_fmt(c['plus_di'],3)} -DI {_fmt(c['minus_di'],3)} RSI {_fmt(c['rsi'],3)} "
              f"| regime {c['regime']} | funding {c['funding_payer']} pays "
              f"{_fmt(c['funding_annualized_pct'],3)}%/yr")


if __name__ == "__main__":
    main()

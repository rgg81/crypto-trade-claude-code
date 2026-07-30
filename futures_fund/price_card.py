"""Candidate price cards (cy78 backlog — anti-hallucination).

When the orchestrator dispatches the debate / Research Manager / Trader, those agents reason about
entry/stop/target geometry. If the prompt carries only a prose DEBATE SUMMARY (not the brief's real
numbers), an agent can hallucinate the price — cy78 the RM priced XMR at ~$185 when the real price
was ~$387 (a 2x error; the Trader caught it only because the orchestrator re-injected ground truth,
and the gate's proposal_audit would have dropped a $185 market order anyway).

`price_card` extracts a compact, deterministic slice of a symbol's brief — the exact levels needed
to build geometry — so the orchestrator can paste the REAL numbers into every reasoning prompt.
Pure, read-only, non-protected; tolerant of missing fields (absent -> None, never raises)."""
from __future__ import annotations

from futures_fund.symbols import sym_key

# the decision-relevant deterministic fields lifted verbatim from a brief (None when absent)
_CARD_FIELDS = (
    "last_close", "mark_price", "atr",
    "swing_high", "swing_low", "dist_to_swing_high_pct", "dist_to_swing_low_pct",
    "adx", "plus_di", "minus_di", "rsi", "ema20_slope", "ema50_slope",
    "regime", "trend_direction",
    "funding_payer", "funding_annualized_pct", "funding_rate",
    # baseline-relative carry read (cy309): payer/annualized alone label the 10.95%/yr
    # zero-information baseline as "longs pay" — surface the premium classification too.
    "funding_premium", "funding_vs_baseline",
    "long_short_ratio", "long_account", "oi_change",
)


def price_card(brief: dict) -> dict:
    """A compact, deterministic extract of one symbol's brief — the REAL levels (price, ATR, swings,
    DMI/RSI, funding direction) an agent needs to build geometry without guessing. Missing fields
    are carried as None so a partial brief never raises."""
    b = brief if isinstance(brief, dict) else {}
    card = {"symbol": b.get("symbol")}
    for f in _CARD_FIELDS:
        card[f] = b.get(f)
    return card


def price_cards(context_or_briefs, symbols=None) -> list[dict]:
    """Build price cards for a cycle's candidates. Accepts a context dict ({"briefs": [...]}) or a
    bare list of briefs. When `symbols` is given, returns only those (preserving brief order); else
    every brief. Malformed input -> [] (never raises)."""
    if isinstance(context_or_briefs, dict):
        briefs = context_or_briefs.get("briefs")
    else:
        briefs = context_or_briefs
    if not isinstance(briefs, list):
        return []
    wanted = {sym_key(s) for s in symbols} if symbols is not None else None
    out: list[dict] = []
    for b in briefs:
        if not isinstance(b, dict):
            continue
        if wanted is not None and sym_key(b.get("symbol")) not in wanted:
            continue
        out.append(price_card(b))
    return out

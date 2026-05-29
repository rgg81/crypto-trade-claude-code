from __future__ import annotations

from collections import defaultdict

from futures_fund.contracts import AnalystReport

_STANCE_SIGN = {"bullish": 1.0, "bearish": -1.0, "neutral": 0.0}


def symbol_conviction(reports: list[AnalystReport]) -> float:
    """Net directional stance (signed, confidence-weighted) times the number of agents who took
    a non-neutral side — rewards both strength and agreement."""
    net = sum(_STANCE_SIGN[r.stance] * r.confidence for r in reports)
    agreement = sum(1 for r in reports if r.stance != "neutral")
    return abs(net) * agreement


def screen_reports(reports: list[AnalystReport], top_n: int) -> list[str]:
    """Group analyst reports by symbol, rank by conviction, return the top-N symbols (strongest
    first). Symbols with zero conviction (all-neutral) are dropped — the §3.1 funnel."""
    by_symbol: dict[str, list[AnalystReport]] = defaultdict(list)
    for r in reports:
        by_symbol[r.symbol].append(r)
    scored = [(sym, symbol_conviction(rs)) for sym, rs in by_symbol.items()]
    scored = [(sym, c) for sym, c in scored if c > 0.0]
    scored.sort(key=lambda x: x[1], reverse=True)
    return [sym for sym, _ in scored[:top_n]]

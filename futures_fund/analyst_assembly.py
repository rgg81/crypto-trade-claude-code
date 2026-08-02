"""Canonical analyst-reports assembly + defensive normalization (cy77/cy78 backlog).

The desk's funnel (screen -> news-fold -> debate) consumes `analyst_reports.json` as a FLAT list of
`AnalystReport` items, each tagged with its `agent` and — for the News analyst — carrying the
desk-wide `risk_off_flag` at `signals.risk_off_flag`. cy77 shipped a hand-shaped DICT-of-agent-lists
instead, which silently made `screen_step` return EMPTY and degraded the news fold to None. This
module removes both footguns:

  - `assemble_analyst_reports(...)` is the BUILD path: hand it the four agents' raw per-symbol lists
    and it emits the canonical, contract-validated flat list (so the orchestrator can't mis-shape).
  - `normalize_reports(...)` is the DEFENSE the consumers call: a flat list, a {"reports": [...]}
    wrapper, or a dict-of-agent-lists all collapse to the same agent-tagged flat list (recovering a
    desk-wide news flag), so a mis-shape degrades GRACEFULLY+LOUD instead of silently no-opping.

Pure, non-protected, fail-safe on the defense path; fail-LOUD on the build path (an un-coercible
item is the orchestrator's bug to fix, not something to swallow).
"""
from __future__ import annotations

from futures_fund.contracts import AnalystReport

AGENT_KEYS = ("technical", "derivatives", "news", "sentiment")

# stance synonyms -> the AnalystReport Stance literal (bullish/bearish/neutral). The desk's analysts
# emit bullish/bearish, but long/short/buy/sell are accepted so a directional synonym never silently
# collapses to neutral.
_STANCE_MAP = {
    "bullish": "bullish", "long": "bullish", "buy": "bullish",
    "bearish": "bearish", "short": "bearish", "sell": "bearish",
    "neutral": "neutral", "flat": "neutral", "hold": "neutral",
}


def _norm_stance(v) -> str:
    # An UNRECOGNIZED stance must default to NEUTRAL, never a side — defaulting to a direction would
    # inject a long/short bias (HARD RULE 5: kill any directional bias in code). Symmetric.
    return _STANCE_MAP.get(str(v).strip().lower(), "neutral") if v is not None else "neutral"


def _norm_confidence(item: dict) -> float:
    """Prefer `confidence`; fall back to `conviction`; clamp to [0,1]; default 0.0. Resolve on
    None-OR-absent (not just absent): LLM/hand-shaped JSON often emits `confidence: null` next to a
    real `conviction`, and dict.get(key, default) returns the default ONLY when the key is ABSENT —
    so a present-but-null `confidence` would otherwise shadow `conviction` and collapse it to 0.0
    (silently dropping a high-conviction symbol from the screen — the cy77 footgun)."""
    raw = item.get("confidence")
    if raw is None:
        raw = item.get("conviction")
    if raw is None:
        return 0.0
    try:
        return max(0.0, min(1.0, float(raw)))
    except (TypeError, ValueError):
        return 0.0


def _norm_key_points(item: dict) -> list[str]:
    kp = item.get("key_points")
    if isinstance(kp, list) and kp:
        return [str(x) for x in kp]
    thesis = item.get("thesis")
    return [str(thesis)] if thesis else []


# agent-specific extras worth preserving into `signals` (they ride through AnalystReport.extra
# anyway, but keeping them in signals keeps the top-level shape clean for downstream readers).
_EXTRA_KEYS = ("squeeze_risk", "contrarian_note", "holding_intact")


def _canonical_item(item: dict, agent: str, news_risk_off_flag=None) -> dict:
    """One raw per-symbol analyst dict -> a canonical, contract-valid AnalystReport dict."""
    if not isinstance(item, dict):
        raise ValueError(f"{agent} report item is not a dict: {item!r}")
    sym = item.get("symbol")
    if not sym:
        raise ValueError(f"{agent} report item missing 'symbol': {item!r}")
    signals = dict(item.get("signals") or {})
    for k in _EXTRA_KEYS:
        if k in item and k not in signals:
            signals[k] = item[k]
    if agent == "news" and news_risk_off_flag is not None:
        # OR-in (never setdefault): the desk-wide flag must DOMINATE for risk-off — a True can't be
        # silently dropped by an item's own False (the fold is asymmetric: any flag ADDS risk-off,
        # never lifts). A True item flag likewise survives a desk-wide False.
        signals["risk_off_flag"] = bool(news_risk_off_flag) or bool(signals.get("risk_off_flag"))
    out = {
        "agent": agent,
        "symbol": sym,
        "stance": _norm_stance(item.get("stance")),
        "confidence": _norm_confidence(item),
        "key_points": _norm_key_points(item),
        "signals": signals,
    }
    AnalystReport.model_validate(out)   # fail-LOUD on the build path
    return out


def assemble_analyst_reports(technical, derivatives, news, sentiment,
                             *, news_risk_off_flag=None) -> list[dict]:
    """Build the CANONICAL flat `analyst_reports.json` payload from the four agents' raw per-symbol
    lists. Maps conviction->confidence and thesis->key_points, normalizes stance synonyms, stamps
    the desk-wide `risk_off_flag` onto every news item's signals, and validates each item against
    the AnalystReport contract. Raises ValueError on an un-coercible item (orchestrator's bug).

    PSEUDO-SYMBOL rows (a leading underscore, e.g. the News analyst's `_AGGREGATE` carrier for the
    desk-wide risk_off flag) are DROPPED here (cy318). They describe the market, not a tradeable
    instrument — no brief, no market, no price — so letting one through means a downstream consumer
    can mistake it for a candidate. At cy318 `_AGGREGATE` ranked high enough on stance/confidence
    that `screen_cli` returned it as one of the five screened names, displacing a real one and
    offering the RM/Trader something unbuyable. Safe for the regime fold: `aggregate_news_risk_off`
    folds over EVERY news row and the flag is stamped on all of them, so the carrier is redundant
    by the time it reaches here."""
    out: list[dict] = []
    for agent, rows in (("technical", technical), ("derivatives", derivatives),
                        ("news", news), ("sentiment", sentiment)):
        for item in (rows or []):
            sym = item.get("symbol") if isinstance(item, dict) else None
            if isinstance(sym, str) and sym.startswith("_"):
                continue
            out.append(_canonical_item(item, agent, news_risk_off_flag=news_risk_off_flag))
    return out


def canonicalize_lenient(reports) -> list[dict]:
    """Flatten ANY shape (via `normalize_reports`) AND canonicalize each item (conviction->
    confidence, stance synonyms, thesis->key_points) so every returned dict validates against
    AnalystReport — the form `screen_step` needs. LENIENT: an un-coercible item (e.g. no symbol) is
    skipped, not raised — this is the defensive path (the build path `assemble_*` is loud)."""
    out: list[dict] = []
    for item in normalize_reports(reports):
        try:
            out.append(_canonical_item(item, (item.get("agent") if isinstance(item, dict)
                                              else None) or "technical"))
        except ValueError:
            continue
    return out


def normalize_reports(reports) -> list[dict]:
    """Collapse ANY accepted analyst_reports shape into a flat, agent-tagged list — the DEFENSE the
    consumers (`screen_step`, `aggregate_news_risk_off`) call so a mis-shaped payload degrades
    gracefully instead of silently no-opping. Accepts: a flat list (returned as-is), a
    {"reports": [...]} wrapper, or a dict-of-agent-lists ({"technical": [...], "news": [...]}) which
    is flattened with each item tagged by its key and a top-level desk-wide news flag
    (`news_risk_off` / `signals.risk_off_flag`) recovered onto news items. Anything else -> []."""
    if isinstance(reports, list):
        return reports
    if not isinstance(reports, dict):
        return []
    has_agent_keys = any(isinstance(reports.get(k), list) for k in AGENT_KEYS)
    inner = reports.get("reports")
    # Only treat {"reports": [...]} as the wrapper when there is NO agent-keyed data — otherwise a
    # dict that carries BOTH agent lists AND a stray "reports" key would silently drop every agent
    # report in favor of `reports["reports"]`.
    if isinstance(inner, list) and not has_agent_keys:
        return inner
    desk_flag = reports.get("news_risk_off")
    if desk_flag is None and isinstance(reports.get("signals"), dict):
        desk_flag = reports["signals"].get("risk_off_flag")
    out: list[dict] = []
    for key in AGENT_KEYS:
        rows = reports.get(key)
        if not isinstance(rows, list):
            continue
        for item in rows:
            if not isinstance(item, dict):
                continue
            it = dict(item)
            it.setdefault("agent", key)
            if key == "news" and desk_flag is not None:
                sig = dict(it.get("signals") or {})
                # OR-in: a True desk-wide flag dominates (risk-off only adds, never lifts); a True
                # item flag survives a desk-wide False. Mirrors _canonical_item's precedence.
                sig["risk_off_flag"] = bool(desk_flag) or bool(sig.get("risk_off_flag"))
                it["signals"] = sig
            out.append(it)
    return out

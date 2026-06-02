"""Closes the discarded-signal gap in the regime arbiter.

The deterministic regime (`futures_fund.regime`) needs a market-wide `news_risk_off` boolean, but
at preflight time only raw RSS headlines exist — so the news term was always degraded to 0. The
News ANALYST (Phase 4) already converts those same headlines into a per-symbol
`signals.risk_off_flag`, but its output was consumed NOWHERE. This module aggregates that analyst
signal into the tri-state boolean the regime consumes, run in a Phase 4.6 re-classification.

TRI-STATE (deliberate — distinguishes a clear tape from a missing feed):
  None  -> news genuinely UNAVAILABLE: a 'news feed' warning, OR zero news reports (analyst pass
           absent, e.g. a halted/stand-down cycle). Reproduces today's fail-closed
           'news_flag_missing' (news_term=0, degraded).
  False -> news pass ran and NO catalyst was flagged. news_term=0 too, but NOT degraded — this is
           the fix for the old False/missing collapse that logged a spurious 'news_flag_missing'.
  True  -> at least one news report flagged risk_off. Per the desk's chosen rule, ANY single flag
           (major or non-major) is desk-wide: the News analyst raises the flag only on a credible
           market-wide or symbol-specific SHOCK (a high bar), and a per-symbol shock (hack /
           delisting / exploit) is desk-wide gap-risk.

FAIL-CLOSED (enforced in regime.py): news is EXCLUDED from the deterministic LABEL entirely — the
label (and thus persistence -> confirmed, the advisory conviction strength) is computed from a
news-blind score (breadth + BTC + F&G). A True flag colors only the ADVISORY score the agents read;
it can NEVER move the label, satisfy quorum/persistence, or manufacture a confirmed risk-off. This
is deliberate: news_risk_off is an LLM-judged analyst signal, and the desk's invariant is that the
regime read derives only from the hard quantitative chain. (Within the advisory score the asymmetry
also holds: news_term in [-1, 0] only deepens risk-off, never lifts toward risk-on.)
"""
from __future__ import annotations


_TRUTHY_STR = {"1", "true", "yes", "y", "t"}


def _flag_set(report: dict) -> bool:
    """True when risk_off_flag denotes 'set'. analyst_reports.json is LLM-authored JSON, so the flag
    can arrive as the int/bool 1/True OR as a string ('1'/'true'/'yes'); accept all of those and
    reject 0 / '0' / 'false' / '' / None / absent. Missing a real shock by being too strict is the
    unsafe failure here (it silently drops the regime's only news signal), so we coerce generously
    but never raise."""
    sig = report.get("signals")
    if not isinstance(sig, dict):
        return False
    v = sig.get("risk_off_flag")
    if isinstance(v, str):
        return v.strip().lower() in _TRUTHY_STR
    if isinstance(v, bool):
        return v
    return v == 1


def aggregate_news_risk_off(analyst_reports, briefs=None, warnings=None) -> bool | None:
    """Fold the News analyst's per-symbol risk_off_flag into one market-wide tri-state boolean.

    `briefs` is accepted for interface symmetry with the deterministic core (the 'any 1 flag' rule
    does not need it). Never raises — any malformed input degrades to None (fail-closed)."""
    try:
        warns = warnings or []
        # The feed itself being down beats any (possibly stale) flagged report -> degraded.
        if any(isinstance(w, str) and "news feed" in w for w in warns):
            return None
        reports = analyst_reports if isinstance(analyst_reports, list) else []
        news = [r for r in reports if isinstance(r, dict) and r.get("agent") == "news"]
        if not news:
            return None  # analyst news pass absent -> degraded (today's behavior)
        # 'any 1 flag -> desk-wide' (the desk's chosen rule). The pass ran, so a clean read is a
        # real False (news_term=0, NOT degraded), distinct from a missing feed (None).
        return any(_flag_set(r) for r in news)
    except Exception:  # noqa: BLE001 — classification must never break the cycle
        return None

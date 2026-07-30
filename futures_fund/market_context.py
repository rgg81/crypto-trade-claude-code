from __future__ import annotations

from futures_fund.config import Settings
from futures_fund.vendors import fetch_fear_greed, fetch_macro, fetch_news, fetch_reddit

_FRED_SERIES_LABELS = {"DTWEXBGS": "broad_dollar", "DGS10": "ust_10y",
                       "FEDFUNDS": "fed_funds", "CPIAUCSL": "cpi"}


def social_engagement_degraded(social: dict | None) -> bool:
    """True when the reddit scrape returned POSTS but every one carries zero engagement.

    The degrade detector used to fire only on ZERO POSTS, so a feed that kept returning post
    TITLES while `score`/`num_comments` came back identically 0 sailed through with an empty
    `warnings` list — it ran that way for 11+ cycles (cy299-310) until the Sentiment analyst
    noticed empirically and capped its own conviction by hand. Titles stay readable (tone is still
    signal), but mention counts and `score_sum` are meaningless, so the agents must be told.
    Fail-safe: returns False on empty/missing input, where the no-posts warning already owns the
    case, and False when engagement keys are simply absent (never invent a degrade)."""
    posts = (social or {}).get("posts") or []
    if not posts:
        return False
    seen_metric = False
    for p in posts:
        if not isinstance(p, dict):
            continue
        for key in ("score", "num_comments"):
            if key in p and p[key] is not None:
                seen_metric = True
                try:
                    if float(p[key]) != 0.0:
                        return False
                except (TypeError, ValueError):
                    return False
    return seen_metric


def build_market_context(http_client, settings: Settings, fred_key: str | None) -> dict:
    """Assemble the market-wide context (news + Fear&Greed + macro) the news/sentiment/macro
    agents need. Each feed degrades independently: a failure omits it and records a warning so
    the agents cap conviction (mission §5)."""
    warnings: list[str] = []

    try:
        fg = fetch_fear_greed(http_client)
        fear_greed = {"value": fg.value, "classification": fg.classification}
    except Exception:
        fear_greed = None
        warnings.append("sentiment feed (Fear&Greed) unavailable — cap conviction")

    try:
        items = fetch_news(http_client, settings.data.news_rss_sources,
                           symbols=settings.symbols, per_source=10)
        news = [i.model_dump() for i in items]
        if not news:
            warnings.append("news feed returned no items — treat catalysts as unknown")
    except Exception:
        news = []
        warnings.append("news feed unavailable — cap conviction on catalysts")

    macro = fetch_macro(http_client, list(settings.data.fred_series), fred_key)
    if not macro:
        warnings.append("macro feed (FRED) unavailable — no DXY/yields/Fed read")

    # (helper defined at module level — see social_engagement_degraded)
    # Reddit social-sentiment scrape (keyless): real crowd CONTENT per symbol for the Sentiment
    # analyst, beyond the single Fear&Greed number. Degrades to empty if reddit blocks the read.
    try:
        social = fetch_reddit(http_client, list(settings.data.reddit_subreddits),
                              symbols=settings.symbols)
        if not social.get("posts"):
            warnings.append("social feed (reddit) returned no posts — cap social-sentiment read")
        elif social_engagement_degraded(social):
            warnings.append("social feed (reddit) returned posts but engagement is uniformly "
                            "zero (score/num_comments all 0) — titles readable, but treat "
                            "mention counts and score_sum as NO SIGNAL; cap social conviction")
    except Exception:
        social = {"posts": [], "mentions": {}}
        warnings.append("social feed (reddit) unavailable — cap social-sentiment read")

    return {"fear_greed": fear_greed, "news": news, "macro": macro, "social": social,
            "macro_labels": _FRED_SERIES_LABELS, "warnings": warnings}

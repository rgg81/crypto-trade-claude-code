from __future__ import annotations

from futures_fund.config import Settings
from futures_fund.vendors import (
    fetch_fear_greed,
    fetch_macro_dated,
    fetch_news,
    fetch_reddit,
)

_FRED_SERIES_LABELS = {"DTWEXBGS": "broad_dollar", "DGS10": "ust_10y",
                       "FEDFUNDS": "fed_funds", "CPIAUCSL": "cpi"}


def social_engagement_available(social: dict | None) -> bool:
    """True when the scrape used a path that CAN carry engagement at all (reddit's /hot.json).

    `_posts_for_sub` tries /hot.json (carries score + num_comments) and falls back to /.rss, which
    structurally carries NEITHER. A cy320 live probe found BOTH www.reddit.com and old.reddit.com
    return 403 for keyless reads, so the desk is permanently on the .rss path. Posts tagged
    `source_kind == "rss"` therefore have no engagement to lose — that is the feed we have, not a
    fault. Unlabelled posts are treated as available so the pre-cy320 rule still applies to them.
    Fail-safe: False on empty/missing input."""
    posts = [p for p in ((social or {}).get("posts") or []) if isinstance(p, dict)]
    if not posts:
        return False          # no usable post -> do not CLAIM a capability we cannot verify
    kinds = {p.get("source_kind") for p in posts}
    kinds.discard(None)
    return not kinds or kinds != {"rss"}


def social_engagement_degraded(social: dict | None) -> bool:
    """True when the reddit scrape returned POSTS but every one carries zero engagement.

    cy320: this now fires ONLY when engagement was actually AVAILABLE (the /hot.json path) and
    came back flat. Previously it also fired on the .rss fallback, where score/num_comments do not
    exist by construction — so it flagged "DEGRADED" on cy317, 318, 319 AND 320, and the Sentiment
    analyst dutifully capped its own conviction to ~0.15 every time for a condition that is simply
    the feed's shape. A warning that fires every single cycle carries no information (the d6da6f70
    silent-off-switch pattern, inverted into an always-on alarm) and it CONFLATES two different
    states: "we never had this metric" (structural — use `social_engagement_available`) versus
    "we had it and it went flat" (a real anomaly worth acting on, which this keeps detecting).

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
    if not social_engagement_available(social):
        return False          # .rss path: no engagement by construction, not an anomaly
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

    # Carry each series' OBSERVATION DATE alongside its value (cy327). A bare number cannot say
    # whether it is fresh, so an analyst comparing cycles has no way to tell "unchanged because
    # this series is monthly" from "unchanged because the feed is stuck" — see fetch_macro_dated.
    macro, macro_asof = fetch_macro_dated(http_client, list(settings.data.fred_series), fred_key)
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
            "macro_asof": macro_asof, "macro_labels": _FRED_SERIES_LABELS,
            "warnings": warnings}

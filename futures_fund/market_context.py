from __future__ import annotations

from futures_fund.config import Settings
from futures_fund.vendors import fetch_fear_greed, fetch_macro, fetch_news

_FRED_SERIES_LABELS = {"DTWEXBGS": "broad_dollar", "DGS10": "ust_10y",
                       "FEDFUNDS": "fed_funds", "CPIAUCSL": "cpi"}


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

    return {"fear_greed": fear_greed, "news": news, "macro": macro,
            "macro_labels": _FRED_SERIES_LABELS, "warnings": warnings}

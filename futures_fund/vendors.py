from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel

FNG_URL = "https://api.alternative.me/fng/"
FRED_URL = "https://api.stlouisfed.org/fred/series/observations"


class FearGreed(BaseModel):
    value: int
    classification: str
    ts: datetime


class NewsItem(BaseModel):
    title: str
    url: str
    published_at: str
    source: str
    kind: str
    instruments: list[str]
    votes_positive: int = 0
    votes_negative: int = 0


def parse_fear_greed(payload: dict) -> FearGreed:
    d = payload["data"][0]
    return FearGreed(
        value=int(d["value"]),
        classification=d["value_classification"],
        ts=datetime.fromtimestamp(int(d["timestamp"]), tz=UTC),
    )


_ATOM = "{http://www.w3.org/2005/Atom}"
_ALIASES = {
    "BTC": ("btc", "bitcoin"), "ETH": ("eth", "ethereum"), "SOL": ("sol", "solana"),
    "BNB": ("bnb", "binance coin"), "XRP": ("xrp", "ripple"), "DOGE": ("doge", "dogecoin"),
    "ADA": ("ada", "cardano"), "AVAX": ("avax", "avalanche"),
}


def _base(symbol: str) -> str:
    # "BTC/USDT:USDT" -> "BTC"; "BTCUSDT" -> "BTC"
    s = symbol.split("/")[0]
    return s[:-4] if s.endswith("USDT") else s


def tag_instruments(title: str, symbols: list[str]) -> list[str]:
    """Which of `symbols` (bases or unified) a headline mentions, by ticker or full name."""
    t = title.lower()
    out: list[str] = []
    for sym in symbols:
        b = _base(sym)
        kws = (b.lower(),) + _ALIASES.get(b, ())
        if any(k in t for k in kws) and b not in out:
            out.append(b)
    return out


def _rss_text(el, tag: str) -> str | None:
    for cand in (tag, _ATOM + tag):
        e = el.find(cand)
        if e is not None:
            if e.text and e.text.strip():
                return e.text.strip()
            if e.get("href"):
                return e.get("href")
    return None


def parse_rss(content: bytes, source: str, symbols: list[str]) -> list[NewsItem]:
    """Parse an RSS/Atom feed (namespace-aware) into NewsItems. Returns [] on malformed XML."""
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return []
    nodes = root.findall(".//item") or root.findall(f".//{_ATOM}entry")
    items: list[NewsItem] = []
    for n in nodes:
        title = _rss_text(n, "title")
        if not title:
            continue
        items.append(NewsItem(
            title=title,
            url=_rss_text(n, "link") or "",
            published_at=_rss_text(n, "pubDate") or _rss_text(n, "published")
            or _rss_text(n, "updated") or "",
            source=source,
            kind="news",
            instruments=tag_instruments(title, symbols),
        ))
    return items


def fetch_news(
    client, sources: list[str], symbols: list[str], per_source: int = 10
) -> list[NewsItem]:
    """Fetch + parse multiple keyless RSS news feeds; skip any source that errors; dedupe by
    title."""
    seen: set[str] = set()
    out: list[NewsItem] = []
    for url in sources:
        try:
            r = client.get(url, headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            src = url.split("//")[-1].split("/")[0]
            for item in parse_rss(r.content, source=src, symbols=symbols)[:per_source]:
                if item.title not in seen:
                    seen.add(item.title)
                    out.append(item)
        except Exception:
            continue  # graceful: a dead/blocked source must not break the cycle
    return out


def fetch_macro(client, series: list[str], api_key: str | None) -> dict[str, float]:
    """Latest value per FRED series (DXY/yields/Fed/CPI). Empty dict if no key (graceful)."""
    if not api_key:
        return {}
    out: dict[str, float] = {}
    for sid in series:
        try:
            r = client.get(FRED_URL, params={"series_id": sid, "api_key": api_key,
                                              "file_type": "json", "sort_order": "desc",
                                              "limit": 1})
            r.raise_for_status()
            # pick the latest observation by ISO date — order-independent (don't trust API order)
            vals = parse_fred(r.json())  # [(date, value)], skips "."
            if vals:
                out[sid] = max(vals, key=lambda dv: dv[0])[1]
        except Exception:
            continue
    return out


def parse_fred(payload: dict) -> list[tuple[str, float]]:
    out: list[tuple[str, float]] = []
    for o in payload.get("observations", []):
        if o["value"] == ".":  # FRED missing-value sentinel
            continue
        out.append((o["date"], float(o["value"])))
    return out


def fetch_fear_greed(client, limit: int = 1) -> FearGreed:
    r = client.get(FNG_URL, params={"limit": limit, "format": "json"})
    r.raise_for_status()
    return parse_fear_greed(r.json())


def fetch_fred_series(client, series_id: str, api_key: str, observation_start: str | None = None
                      ) -> list[tuple[str, float]]:
    params = {"series_id": series_id, "api_key": api_key, "file_type": "json", "sort_order": "asc"}
    if observation_start:
        params["observation_start"] = observation_start
    r = client.get(FRED_URL, params=params)
    r.raise_for_status()
    return parse_fred(r.json())


def archive_jsonl(path, records: list[dict], key: str = "timestamp") -> int:
    """Append `records` to a JSONL file, deduping by `key` against existing rows.
    Returns the number of new rows written. Used to self-archive the 30-day-limited
    OI / long-short endpoints into durable history (spec §10)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    seen: set = set()
    if p.exists():
        for line in p.read_text().splitlines():
            if line.strip():
                seen.add(json.loads(line).get(key))
    written = 0
    with p.open("a") as f:
        for rec in records:
            k = rec.get(key)
            if k is not None and k in seen:
                continue
            f.write(json.dumps(rec, default=str) + "\n")
            if k is not None:
                seen.add(k)
            written += 1
    return written

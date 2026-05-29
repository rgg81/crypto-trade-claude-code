from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel

FNG_URL = "https://api.alternative.me/fng/"
CRYPTOPANIC_URL = "https://cryptopanic.com/api/developer/v2/posts/"
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


def parse_cryptopanic(payload: dict) -> list[NewsItem]:
    items: list[NewsItem] = []
    for p in payload.get("results", []):
        source = (p.get("source") or {}).get("title", "")
        # v2 uses 'instruments'; tolerate legacy v1 'currencies'
        coins = p.get("instruments") or p.get("currencies") or []
        votes = p.get("votes") or {}
        items.append(
            NewsItem(
                title=p["title"],
                url=p.get("url", ""),
                published_at=p["published_at"],
                source=source,
                kind=p.get("kind", "news"),
                instruments=[c["code"] for c in coins],
                votes_positive=int(votes.get("positive", 0)),
                votes_negative=int(votes.get("negative", 0)),
            )
        )
    return items


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


def fetch_cryptopanic(
    client, token: str, currencies: str = "BTC,ETH", kind: str = "news"
) -> list[NewsItem]:
    r = client.get(
        CRYPTOPANIC_URL,
        params={"auth_token": token, "public": "true", "currencies": currencies, "kind": kind},
    )
    r.raise_for_status()
    return parse_cryptopanic(r.json())


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

import json

from futures_fund.vendors import (
    FearGreed,
    NewsItem,
    archive_jsonl,
    fetch_fear_greed,
    fetch_macro,
    fetch_news,
    parse_fear_greed,
    parse_fred,
    parse_rss,
    tag_instruments,
)

_RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel>
<item><title>Bitcoin ETFs bleed $2.8B in record outflow streak</title>
<link>https://x/news/1</link><pubDate>Fri, 29 May 2026 14:20:32 +0000</pubDate></item>
<item><title>Ethereum downside pressure remains as $1.8K becomes key</title>
<link>https://x/news/2</link><pubDate>Fri, 29 May 2026 15:50:08 +0000</pubDate></item>
<item><title>Regulators weigh new stablecoin rules</title>
<link>https://x/news/3</link><pubDate>Fri, 29 May 2026 13:00:00 +0000</pubDate></item>
</channel></rss>"""


def test_tag_instruments_matches_base_and_alias():
    assert tag_instruments("Bitcoin ETFs bleed", ["BTC", "ETH"]) == ["BTC"]
    assert tag_instruments("Ethereum downside; BTC dips", ["BTC", "ETH"]) == ["BTC", "ETH"]
    assert tag_instruments("Regulators weigh stablecoin rules", ["BTC", "ETH"]) == []


def test_parse_rss_extracts_items_and_tags():
    items = parse_rss(_RSS, source="CoinDesk", symbols=["BTC", "ETH"])
    assert len(items) == 3 and all(isinstance(i, NewsItem) for i in items)
    assert items[0].title.startswith("Bitcoin ETFs")
    assert items[0].source == "CoinDesk" and items[0].url == "https://x/news/1"
    assert items[0].instruments == ["BTC"]
    assert items[1].instruments == ["ETH"]


def test_parse_rss_tolerates_garbage():
    assert parse_rss(b"not xml", source="X", symbols=["BTC"]) == []


class _Resp:
    def __init__(self, *, content=b"", payload=None, status=200):
        self.content = content
        self._payload = payload
        self.status_code = status
    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")
    def json(self):
        return self._payload


class _NewsClient:
    def __init__(self, by_url):
        self.by_url = by_url
    def get(self, url, params=None, **kw):
        return self.by_url.get(url, _Resp(status=404))


def test_fetch_news_merges_sources_and_dedupes():
    c = _NewsClient({"u1": _Resp(content=_RSS), "u2": _Resp(content=_RSS)})  # same feed twice
    items = fetch_news(c, sources=["u1", "u2"], symbols=["BTC", "ETH"], per_source=10)
    assert len(items) == 3  # deduped by title across the two sources


def test_fetch_news_skips_failing_source():
    c = _NewsClient({"ok": _Resp(content=_RSS), "bad": _Resp(status=503)})
    items = fetch_news(c, sources=["bad", "ok"], symbols=["BTC"], per_source=10)
    assert len(items) == 3  # bad source skipped, good one parsed


def test_fetch_macro_returns_latest_values():
    obs = {"observations": [{"date": "2026-05-26", "value": "4.47"},
                            {"date": "2026-05-27", "value": "4.48"}]}
    c = _NewsClient({"https://api.stlouisfed.org/fred/series/observations": _Resp(payload=obs)})
    macro = fetch_macro(c, series=["DGS10"], api_key="k" * 32)
    assert macro["DGS10"] == 4.48  # newest non-missing


def test_fetch_macro_without_key_is_empty():
    assert fetch_macro(_NewsClient({}), series=["DGS10"], api_key=None) == {}


class FakeResp:
    def __init__(self, payload):
        self._p = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._p


class FakeClient:
    def __init__(self, payload):
        self._p = payload
        self.last = None

    def get(self, url, params=None, **kw):
        self.last = (url, params)
        return FakeResp(self._p)


def test_parse_fear_greed_casts_strings_to_typed():
    payload = {"data": [{"value": "23", "value_classification": "Extreme Fear",
                         "timestamp": "1780012800"}]}
    fg = parse_fear_greed(payload)
    assert isinstance(fg, FearGreed)
    assert fg.value == 23 and fg.classification == "Extreme Fear"
    assert str(fg.ts.tzinfo) == "UTC"


def test_parse_fred_skips_missing_dot_values():
    payload = {"observations": [
        {"date": "2026-05-27", "value": "4.5"},
        {"date": "2026-05-28", "value": "."},      # weekend/holiday missing
        {"date": "2026-05-29", "value": "4.6"},
    ]}
    obs = parse_fred(payload)
    assert obs == [("2026-05-27", 4.5), ("2026-05-29", 4.6)]


def test_fetch_fear_greed_calls_endpoint_and_parses():
    client = FakeClient({"data": [{"value": "50", "value_classification": "Neutral",
                                   "timestamp": "1780012800"}]})
    fg = fetch_fear_greed(client, limit=1)
    assert fg.value == 50
    assert client.last[0] == "https://api.alternative.me/fng/"
    assert client.last[1]["limit"] == 1


def test_archive_jsonl_appends_and_dedupes(tmp_path):
    path = tmp_path / "oi.jsonl"
    rows = [{"timestamp": 1, "oi": 10.0}, {"timestamp": 2, "oi": 11.0}]
    assert archive_jsonl(path, rows, key="timestamp") == 2
    # re-archiving overlapping data writes only the new record
    rows2 = [{"timestamp": 2, "oi": 11.0}, {"timestamp": 3, "oi": 12.0}]
    assert archive_jsonl(path, rows2, key="timestamp") == 1
    lines = [json.loads(x) for x in path.read_text().splitlines() if x.strip()]
    assert [r["timestamp"] for r in lines] == [1, 2, 3]


def test_archive_jsonl_keeps_rows_without_key(tmp_path):
    path = tmp_path / "x.jsonl"
    # records lack the dedup key -> all kept, never silently collapsed to one "None"
    assert archive_jsonl(path, [{"a": 1}, {"a": 2}], key="timestamp") == 2

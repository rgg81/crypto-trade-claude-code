import json

from futures_fund.vendors import (
    FearGreed,
    NewsItem,
    archive_jsonl,
    fetch_fear_greed,
    parse_cryptopanic,
    parse_fear_greed,
    parse_fred,
)


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


def test_parse_cryptopanic_v2_uses_instruments():
    payload = {"results": [
        {"title": "BTC rips", "url": "http://cp/1", "published_at": "2026-05-29T08:00:00Z",
         "kind": "news", "source": {"title": "CoinDesk"},
         "instruments": [{"code": "BTC"}, {"code": "ETH"}],
         "votes": {"positive": 5, "negative": 1}},
    ]}
    items = parse_cryptopanic(payload)
    assert len(items) == 1 and isinstance(items[0], NewsItem)
    assert items[0].source == "CoinDesk"
    assert items[0].instruments == ["BTC", "ETH"]
    assert items[0].votes_positive == 5


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

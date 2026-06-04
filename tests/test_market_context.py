from futures_fund.config import Settings
from futures_fund.market_context import build_market_context

_RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel>
<item><title>Bitcoin ETFs see record outflows</title><link>http://x/1</link>
<pubDate>Fri, 29 May 2026 14:20:32 +0000</pubDate></item></channel></rss>"""
_FNG = {"data": [{"value": "23", "value_classification": "Extreme Fear",
                  "timestamp": "1780012800"}]}
_FRED = {"observations": [{"date": "2026-05-27", "value": "4.48"}]}
_REDDIT = {"data": {"children": [{"kind": "t3", "data": {
    "title": "BTC discussion thread", "selftext": "bitcoin sentiment is fearful",
    "score": 250, "num_comments": 80}}]}}


class _Resp:
    def __init__(self, *, content=b"", payload=None, status=200):
        self.content = content
        self._p = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("http")

    def json(self):
        return self._p


class _Client:
    def __init__(self, fng_fail=False):
        self.fng_fail = fng_fail
    def get(self, url, params=None, **kw):
        if "alternative.me" in url:
            return _Resp(status=500) if self.fng_fail else _Resp(payload=_FNG)
        if "stlouisfed" in url:
            return _Resp(payload=_FRED)
        if "reddit.com" in url:
            return _Resp(payload=_REDDIT)
        return _Resp(content=_RSS)  # any RSS source


def _settings(fred_key=None):
    s = Settings(symbols=["BTC/USDT:USDT", "ETH/USDT:USDT"],
                 news_rss_sources=["http://feed-a", "http://feed-b"])
    return s


def test_market_context_assembles_all_feeds():
    mc = build_market_context(_Client(), _settings(), fred_key="k" * 32)
    assert mc["fear_greed"]["value"] == 23
    assert len(mc["news"]) >= 1 and mc["news"][0]["title"].startswith("Bitcoin ETFs")
    assert mc["macro"]["DGS10"] == 4.48
    # social (reddit) feed assembled: top posts + per-symbol mentions
    assert mc["social"]["posts"] and mc["social"]["mentions"]["BTC"]["count"] >= 1
    assert mc["warnings"] == []


def test_market_context_degrades_without_fred_key():
    mc = build_market_context(_Client(), _settings(), fred_key=None)
    assert mc["macro"] == {}
    assert any("macro" in w.lower() for w in mc["warnings"])


def test_market_context_degrades_when_fear_greed_down():
    mc = build_market_context(_Client(fng_fail=True), _settings(), fred_key="k" * 32)
    assert mc["fear_greed"] is None
    assert any("fear" in w.lower() or "sentiment" in w.lower() for w in mc["warnings"])

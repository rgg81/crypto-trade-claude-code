import json

from futures_fund.vendors import (
    FearGreed,
    NewsItem,
    archive_jsonl,
    fetch_fear_greed,
    fetch_macro,
    fetch_macro_dated,
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


def test_tag_instruments_requires_word_boundaries_not_substrings():
    """cy307: a short ticker matched as a raw SUBSTRING tags every English word containing it.
    ON (Orochi Network) was tagged on 'live on Starknet' / 'banks on public blockchains' and 22
    phantom social mentions, polluting the News + Sentiment feeds for multiple cycles."""
    assert tag_instruments("Rollup goes live on Starknet", ["ON"]) == []
    assert tag_instruments("Traders stay long into London close", ["ON"]) == []
    assert tag_instruments("Beyond the second month, conditions worsen", ["ON"]) == []
    # the genuine mention still tags
    assert tag_instruments("ON rallies 74% on volume", ["ON"]) == ["ON"]
    assert tag_instruments("$ON breaks out", ["ON"]) == ["ON"]
    # and a real base is not swallowed by a longer word (SUI vs 'suit', UNI vs 'university')
    assert tag_instruments("The lawsuit named a bank", ["SUI"]) == []
    assert tag_instruments("University endowment buys", ["UNI"]) == []
    assert tag_instruments("SUI and UNI both dip", ["SUI", "UNI"]) == ["SUI", "UNI"]
    # multi-word aliases keep working
    assert tag_instruments("Binance Coin holders vote", ["BNB"]) == ["BNB"]


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


def test_fetch_macro_asof_reports_the_observation_date():
    """cy327: the value alone cannot tell 'unchanged' from 'stale'. Carry the date too."""
    obs = {"observations": [{"date": "2026-05-26", "value": "4.47"},
                            {"date": "2026-05-27", "value": "4.48"}]}
    c = _NewsClient({"https://api.stlouisfed.org/fred/series/observations": _Resp(payload=obs)})
    macro, asof = fetch_macro_dated(c, series=["DGS10"], api_key="k" * 32)
    assert macro["DGS10"] == 4.48
    assert asof["DGS10"] == "2026-05-27"  # the date OF the value returned, not "now"


def test_fetch_macro_dated_without_key_is_two_empties():
    assert fetch_macro_dated(_NewsClient({}), series=["DGS10"], api_key=None) == ({}, {})


def test_fetch_macro_dated_skips_a_series_that_errors_without_losing_the_others():
    obs = {"observations": [{"date": "2026-05-27", "value": "4.48"}]}
    c = _NewsClient({"https://api.stlouisfed.org/fred/series/observations": _Resp(payload=obs)})
    macro, asof = fetch_macro_dated(c, series=["DGS10"], api_key="k" * 32)
    assert set(macro) == set(asof) == {"DGS10"}  # keys stay in lockstep


def test_fetch_macro_still_returns_the_bare_value_mapping():
    """The legacy value-only contract must not change shape — consumers depend on it."""
    obs = {"observations": [{"date": "2026-05-27", "value": "4.48"}]}
    c = _NewsClient({"https://api.stlouisfed.org/fred/series/observations": _Resp(payload=obs)})
    assert fetch_macro(c, series=["DGS10"], api_key="k" * 32) == {"DGS10": 4.48}


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


_RSS_BODY = b"""<?xml version="1.0"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel>
<item><title>Markets slide on macro fears</title><link>https://x/1</link>
<description>&lt;p&gt;A broad selloff hit majors. &lt;b&gt;Solana&lt;/b&gt; led losers as
funding flipped.&lt;/p&gt;</description>
<pubDate>Fri, 29 May 2026 14:20:32 +0000</pubDate></item>
<item><title>Protocol upgrade ships</title><link>https://x/2</link>
<content:encoded>&lt;div&gt;The Cardano upgrade went live
with no issues.&lt;/div&gt;</content:encoded>
<pubDate>Fri, 29 May 2026 15:50:08 +0000</pubDate></item>
</channel></rss>"""


def test_parse_rss_captures_body_and_strips_html():
    items = parse_rss(_RSS_BODY, source="X", symbols=["SOL", "ADA"])
    # body captured from <description>, HTML stripped, entities decoded
    assert "Solana led losers" in items[0].summary
    assert "<" not in items[0].summary and "&lt;" not in items[0].summary
    # body captured from <content:encoded> on the 2nd item
    assert "Cardano upgrade went live" in items[1].summary


def test_parse_rss_tags_instruments_from_body_not_just_title():
    items = parse_rss(_RSS_BODY, source="X", symbols=["SOL", "ADA"])
    # SOL appears only in the body of item 0 (title is generic) -> still tagged
    assert "SOL" in items[0].instruments
    # ADA appears only in the body of item 1 -> still tagged
    assert "ADA" in items[1].instruments


def test_news_item_summary_defaults_empty():
    n = NewsItem(title="t", url="u", published_at="p", source="s", kind="news", instruments=[])
    assert n.summary == ""


def test_config_has_multiple_news_sources():
    from futures_fund.config import DataSettings
    assert len(DataSettings().news_rss_sources) >= 4  # broadened beyond coindesk+cointelegraph


# ---- reddit social-sentiment scrape (keyless public JSON) ----

def _reddit_payload(children):
    return {"data": {"children": [{"kind": "t3", "data": d} for d in children]}}


_REDDIT = _reddit_payload([
    {"title": "Solana looking strong into the bounce", "selftext": "SOL volume surging",
     "score": 1500, "num_comments": 320},
    {"title": "Is BTC about to capitulate?", "selftext": "bitcoin sub 60k fear everywhere",
     "score": 800, "num_comments": 210},
    {"title": "Daily discussion", "selftext": "general chat about ADA and cardano staking",
     "score": 50, "num_comments": 900},
])


def test_parse_reddit_extracts_posts_and_tags_from_title_and_body():
    from futures_fund.vendors import parse_reddit
    posts = parse_reddit(_REDDIT, subreddit="CryptoCurrency", symbols=["BTC", "SOL", "ADA"])
    assert len(posts) == 3
    assert posts[0].score == 1500 and posts[0].source == "CryptoCurrency"
    assert "SOL" in posts[0].instruments                 # from title+body
    assert "ADA" in posts[2].instruments                 # 'ADA'/'cardano' only in the body


def test_fetch_reddit_aggregates_score_weighted_mentions_and_dedupes():
    from futures_fund.vendors import fetch_reddit
    c = _NewsClient({"https://www.reddit.com/r/CryptoCurrency/hot.json": _Resp(payload=_REDDIT),
                     "https://www.reddit.com/r/CryptoMarkets/hot.json": _Resp(payload=_REDDIT)})
    out = fetch_reddit(c, subreddits=["CryptoCurrency", "CryptoMarkets"],
                       symbols=["BTC", "SOL", "ADA"], per_sub=40)
    assert set(out.keys()) == {"posts", "mentions", "empty_subreddits"}
    assert out["empty_subreddits"] == []          # cy323: every sub loaded
    # deduped by title across the two identical subs
    assert len(out["posts"]) == 3
    # per-symbol mention aggregation, score-weighted
    assert out["mentions"]["SOL"]["count"] == 1 and out["mentions"]["SOL"]["score_sum"] == 1500
    assert out["mentions"]["BTC"]["count"] == 1
    # posts sorted by score desc (top of the sub first)
    assert out["posts"][0]["score"] >= out["posts"][-1]["score"]


def test_fetch_reddit_degrades_gracefully_on_failure():
    from futures_fund.vendors import fetch_reddit
    c = _NewsClient({})   # every sub 404s
    out = fetch_reddit(c, subreddits=["CryptoCurrency"], symbols=["BTC"], per_sub=40,
                       sleep=lambda _s: None)
    assert out["posts"] == [] and out["mentions"] == {}
    # cy323: a sub that yielded nothing is now REPORTED rather than silently swallowed
    assert out["empty_subreddits"] == ["CryptoCurrency"]


def test_config_has_reddit_subreddits():
    from futures_fund.config import DataSettings
    assert len(DataSettings().reddit_subreddits) >= 1


def test_fetch_reddit_falls_back_to_rss_when_json_blocked():
    from futures_fund.vendors import fetch_reddit
    atom = (b'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">'
            b'<entry><title>SOL pumping hard</title><link href="https://r/1"/>'
            b'<content>solana breakout, FOMO building everywhere</content></entry></feed>')
    # /hot.json is NOT in the map -> 404 -> fetch_reddit falls back to the /.rss Atom feed
    c = _NewsClient({"https://www.reddit.com/r/CryptoCurrency/.rss": _Resp(content=atom)})
    out = fetch_reddit(c, subreddits=["CryptoCurrency"], symbols=["SOL"], per_sub=40)
    assert len(out["posts"]) == 1 and out["posts"][0]["title"] == "SOL pumping hard"
    assert out["posts"][0]["score"] == 0           # .rss carries no upvote score
    assert out["mentions"]["SOL"]["count"] == 1


# --- cy318: the reddit-boilerplate ticker collision -----------------------------------------
# `_WORDLIKE_TICKERS` makes English-word tickers require an uppercase/cashtag form, because a
# bare word-boundary match tags the WORD itself (the cy307 ON/Orochi bug). The set was curated
# by hand and MISSED several liquid perps that are extremely common English words -- LINK worst
# of all, because reddit appends "[link] [comments]" boilerplate to EVERY .rss post. At cy318
# the sentiment analyst reported LINK with 17 mentions across 25 posts; all 17 were boilerplate
# and not one referenced Chainlink. Same class: "sand-bagged" -> SAND, "ate cake" -> CAKE.
# The failure mode is a FALSE POSITIVE (phantom mentions polluting the sentiment feed), so this
# list being incomplete is a live bug, not a cosmetic one.

def test_reddit_boilerplate_does_not_tag_chainlink():
    from futures_fund.vendors import tag_instruments
    syms = ["LINK", "BTC"]
    assert tag_instruments("submitted by /u/someone [link] [comments]", syms) == []
    assert tag_instruments("Here is a link to the docs", syms) == []
    assert tag_instruments("check the link in my bio", syms) == []


def test_uppercase_and_cashtag_link_still_tag():
    """The guard must not go too far: a REAL Chainlink mention still has to register."""
    from futures_fund.vendors import tag_instruments
    syms = ["LINK", "BTC"]
    assert tag_instruments("LINK broke out today", syms) == ["LINK"]
    assert tag_instruments("$LINK looking strong", syms) == ["LINK"]
    assert tag_instruments("Chainlink oracle update", syms) == ["LINK"]   # alias path


def test_other_common_english_word_tickers_are_guarded():
    from futures_fund.vendors import tag_instruments
    syms = ["SAND", "APE", "CAKE", "MASK", "BAND", "DASH", "PUMP", "STORY"]
    assert tag_instruments("I sand-bagged it and ate cake", syms) == []
    assert tag_instruments("wear a mask, join the band, dash to the door", syms) == []
    assert tag_instruments("this is a pump and dump story", syms) == []
    # uppercase forms still tag
    assert tag_instruments("SAND and CAKE are up", syms) == ["SAND", "CAKE"]


def test_wordlike_set_covers_the_tickers_that_burned_us():
    from futures_fund.vendors import _WORDLIKE_TICKERS
    for t in ("LINK", "SAND", "APE", "CAKE", "MASK", "BAND", "DASH", "PUMP", "STORY", "PEOPLE"):
        assert t in _WORDLIKE_TICKERS, f"{t} is a common English word and must require uppercase"


def test_wordlike_alias_never_reopens_the_lowercase_hole():
    """A word-like ticker's bare lowercase form must NOT be an alias — aliases match
    case-insensitively, so listing it would undo the uppercase requirement entirely."""
    from futures_fund.vendors import _ALIASES, _WORDLIKE_TICKERS
    for base, aliases in _ALIASES.items():
        if base.upper() in _WORDLIKE_TICKERS:
            assert base.lower() not in [a.lower() for a in aliases], (
                f"{base}: bare lowercase alias re-opens the prose false positive")


# --- cy323: the SECOND subreddit was silently lost to reddit rate-limiting ------------------
# `_posts_for_sub` tries /hot.json then immediately /.rss, so N configured subreddits fire up to
# 2N back-to-back requests. Reddit 429s everything after roughly the first, and the bare
# `except Exception: return []` swallowed it — so `fetch_reddit(['CryptoCurrency','CryptoMarkets'])`
# returned 25 posts ALL from CryptoCurrency and ZERO from CryptoMarkets, every cycle, silently.
# Verified live at cy323 on the production path. The lost sub is not redundant: fetched on its own
# it returned a HYPE mention, a symbol the Sentiment analyst had reported zero coverage on for
# SEVEN straight cycles while it (correctly) complained the feed only ever showed BTC/ETH.
# A configured source that never actually loads is the d6da6f70 silent-off-switch pattern again.

def test_fetch_reddit_paces_requests_between_subreddits():
    """A pause must be taken BETWEEN subs so the later ones are not 429'd away."""
    from futures_fund.vendors import fetch_reddit
    slept: list = []
    calls: list = []

    class _R:
        status_code = 200
        content = b"<rss><channel></channel></rss>"
        def raise_for_status(self): pass
        def json(self): raise ValueError("no json")

    class _C:
        def get(self, url, **kw):
            calls.append(url)
            return _R()

    fetch_reddit(_C(), ["A", "B", "C"], ["BTC"], per_sub=5, sleep=slept.append)
    assert len(slept) >= 2, f"expected a pause between subs, got {slept}"
    assert all(s > 0 for s in slept)


def test_fetch_reddit_reports_subreddits_that_yielded_nothing():
    """Fail-loud: a sub that returns no posts must be surfaced, not silently dropped."""
    from futures_fund.vendors import fetch_reddit

    class _Resp:
        def __init__(self, ok):
            self.status_code = 200 if ok else 429
            self._ok = ok
        content = b"<rss><channel></channel></rss>"
        def raise_for_status(self):
            if not self._ok:
                raise RuntimeError("429")
        def json(self): raise ValueError("no json")

    class _C:
        def __init__(self): self.n = 0
        def get(self, url, **kw):
            self.n += 1
            return _Resp(self.n <= 2)   # only the first sub's attempts succeed

    out = fetch_reddit(_C(), ["GOOD", "BLOCKED"], ["BTC"], per_sub=5, sleep=lambda s: None)
    assert "empty_subreddits" in out
    assert "BLOCKED" in out["empty_subreddits"]


def test_fetch_reddit_sleep_is_injectable_so_tests_stay_fast():
    from futures_fund.vendors import fetch_reddit

    class _C:
        def get(self, url, **kw): raise RuntimeError("blocked")

    out = fetch_reddit(_C(), ["A", "B"], ["BTC"], per_sub=5, sleep=lambda s: None)
    assert out["posts"] == []
    assert set(out["empty_subreddits"]) == {"A", "B"}


# --- cy323: stop spending half the rate-limit budget on a call that always 403s -------------
# cy320 established that reddit 403s keyless /hot.json for this IP on BOTH www and old.reddit.com,
# so the desk ALWAYS falls back to /.rss. But `_posts_for_sub` still tried json first for every
# sub, spending 2 requests per subreddit where 1 would do — and reddit's limit is a shared bucket,
# so that doubled cost is exactly what starved the second configured sub (cy323). After enough
# consecutive json failures we stop attempting it, halving requests per cycle.
def test_json_attempt_is_skipped_after_it_fails_within_one_call():
    """One wasted 403 per RUN, not per subreddit — the budget saving that matters."""
    from futures_fund.vendors import fetch_reddit
    urls: list = []

    class _Resp:
        content = b"<rss><channel></channel></rss>"

        def raise_for_status(self):
            pass

        def json(self):
            raise ValueError("no json")

    class _C:
        def get(self, url, **kw):
            urls.append(url)
            if "hot.json" in url:
                raise RuntimeError("403")
            return _Resp()

    fetch_reddit(_C(), ["A", "B", "C", "D"], ["BTC"], per_sub=5, sleep=lambda _s: None)
    json_calls = [u for u in urls if "hot.json" in u]
    assert len(json_calls) == 1, (
        f"json should be tried once then abandoned for the rest of the call, got {json_calls}")


def test_a_working_json_path_is_used_for_every_sub():
    """Do not disable a path that works."""
    from futures_fund.vendors import fetch_reddit
    payload = {"data": {"children": [
        {"data": {"title": "BTC up", "selftext": "", "score": 5, "num_comments": 2}}]}}
    urls: list = []

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return payload

    class _C:
        def get(self, url, **kw):
            urls.append(url)
            return _Resp()

    fetch_reddit(_C(), ["A", "B", "C"], ["BTC"], per_sub=5, sleep=lambda _s: None)
    assert len([u for u in urls if "hot.json" in u]) == 3
    assert not [u for u in urls if ".rss" in u]

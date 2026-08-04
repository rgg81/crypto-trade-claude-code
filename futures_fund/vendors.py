from __future__ import annotations

import functools
import html
import json
import re
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
    summary: str = ""               # HTML-stripped article body/snippet (not just the title)
    votes_positive: int = 0
    votes_negative: int = 0


class SocialPost(BaseModel):
    """A reddit post the Sentiment analyst reads to gauge crowd CONTENT (not just an index number).
    `score` = net upvotes (the crowd's weight on the post); `summary` = the self-text snippet."""
    title: str
    summary: str = ""
    score: int = 0
    num_comments: int = 0
    # cy320: which fetch path produced this post — "json" carries engagement, "rss" cannot.
    # Lets the degrade detector tell a STRUCTURAL gap from a real flat-engagement anomaly.
    source_kind: str = "json"
    source: str = ""                # the subreddit, e.g. 'CryptoCurrency'
    instruments: list[str] = []


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
    # cy318: full-name aliases for word-like tickers. These are REQUIRED, not decorative — once a
    # ticker joins _WORDLIKE_TICKERS its bare lowercase form stops matching, so without an alias a
    # headline like "Chainlink launches CCIP" would tag NOTHING. Only unambiguous names here; a
    # name that is itself common prose (e.g. "sandbox") would re-introduce the false positive.
    # NOTE: aliases are matched CASE-INSENSITIVELY, so a word-like ticker's own bare lowercase
    # form must NEVER appear here — listing "link" would re-open the very hole this closes.
    "LINK": ("chainlink",), "APE": ("apecoin",),
    "CAKE": ("pancakeswap",), "MASK": ("mask network",),
}


def _base(symbol: str) -> str:
    # "BTC/USDT:USDT" -> "BTC"; "BTCUSDT" -> "BTC"
    s = symbol.split("/")[0]
    return s[:-4] if s.endswith("USDT") else s


@functools.lru_cache(maxsize=4096)
def _kw_re(keyword: str, *, cased: bool) -> re.Pattern[str]:
    """Word-boundary matcher for one ticker/alias. Cached — tagging runs per headline per symbol.

    Boundaries are `\\w`-based rather than `\\b` so a `$TICKER` cashtag still matches while
    `SUIt` / `UNIversity` / `l-ON-g` do not. `cased=True` compiles a case-SENSITIVE pattern."""
    return re.compile(rf"(?<!\w){re.escape(keyword)}(?!\w)", 0 if cased else re.IGNORECASE)


# Tickers that collide with an ordinary English word. For these a lowercase prose hit is almost
# always a false positive ("live on Starknet", "near the highs", "the bank said"), so they demand
# an unambiguous crypto signal — a $cashtag or a standalone UPPERCASE ticker, which is how
# headlines and social posts actually write them ("ON rallies 74%", "$BANK unlock"). Full-name
# aliases ("orochi network") are unaffected and still match case-insensitively.
_WORDLIKE_TICKERS = frozenset({
    "ON", "IN", "IT", "AT", "IS", "BE", "GO", "ME", "MY", "SO", "UP", "US", "OR", "AN", "AS",
    "BY", "DO", "IF", "NO", "OF", "TO", "WE", "HE", "ALL", "ANY", "ARE", "FOR", "NEW", "ONE",
    "OUT", "OWN", "TOP", "TWO", "WIN", "YOU", "AND", "THE", "NOT", "CAN", "HAS", "HAD", "WAS",
    "GET", "SEE", "WHO", "WHY", "HOW", "NOW", "LOW", "BIG", "BUY", "PAY", "RUN", "SET", "SUN",
    "AIR", "ACT", "AGE", "ART", "BAR", "BET", "BIT", "BOX", "GAS", "NEAR", "BANK", "LIVE",
    "REAL", "MOVE", "CORE", "EDGE", "FLOW", "CASH", "FUND", "HOME", "IDEA", "LOOK", "MOON",
    "NEXT", "BEST", "TIME", "LIKE", "JUST", "MORE", "OVER", "BEAT", "RARE", "SAFE", "HOPE",
    # cy318: liquid perps that are ALSO everyday English words. LINK is the worst offender —
    # reddit appends "[link] [comments]" boilerplate to EVERY .rss post, so Chainlink was
    # credited with a mention on essentially every post it ever saw (17 of 25 at cy318, none
    # real). PUMP matters for the same reason in crypto prose ("pump and dump").
    "LINK", "SAND", "APE", "CAKE", "MASK", "BAND", "DASH", "PUMP", "STORY", "PEOPLE",
    "ALPHA", "PORTAL", "DUSK", "WAVES", "IDOL", "BLESS", "GIGGLE", "KEY", "ICE", "TREE",
    "SPELL", "MAGIC", "ORDER", "POINT", "CHESS", "SLEEP", "TURBO", "BOND", "TRUST", "SUPER",
})


def tag_instruments(title: str, symbols: list[str]) -> list[str]:
    """Which of `symbols` (bases or unified) a headline mentions, by ticker or full name.

    Matched on WORD BOUNDARIES, never as a raw substring, and English-word tickers additionally
    require a cashtag/uppercase form. A substring match tags every English word CONTAINING the
    ticker, and a bare word-boundary match still tags the word ITSELF (cy307 — base `ON`/Orochi
    Network was tagged on "live ON Starknet", "banks ON public blockchains", "l-ON-g", "L-ON-don",
    producing 22 phantom social mentions and 10 mis-tagged headlines that polluted the News
    `instruments` and Sentiment `mentions` feeds for multiple cycles until two analysts
    independently flagged it)."""
    out: list[str] = []
    for sym in symbols:
        b = _base(sym)
        aliases = _ALIASES.get(b, ())
        cased = b.upper() in _WORDLIKE_TICKERS
        # the bare ticker: case-sensitive for word-like tickers; aliases always case-insensitive
        hit = _kw_re(b.upper() if cased else b.lower(), cased=cased).search(title)
        if not hit:
            hit = any(_kw_re(a, cased=False).search(title) for a in aliases)
        if hit and b not in out:
            out.append(b)
    return out


_CONTENT = "{http://purl.org/rss/1.0/modules/content/}"  # <content:encoded> full-body namespace
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _clean_html(s: str | None, limit: int = 500) -> str:
    """Strip HTML tags, decode entities, collapse whitespace, truncate — turn an RSS body snippet
    into a plain-text summary the News analyst can read. Empty string on None."""
    if not s:
        return ""
    text = _WS_RE.sub(" ", _TAG_RE.sub(" ", html.unescape(s))).strip()
    return text[:limit].rstrip() + ("…" if len(text) > limit else "")


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
        # Body: RSS <content:encoded> (full) or <description>; Atom <content>/<summary>. The body
        # often names coins the title doesn't, so tag instruments on title + body, and hand the
        # analyst the HTML-stripped snippet — not just the headline.
        raw_body = (_rss_text(n, _CONTENT + "encoded") or _rss_text(n, "encoded")
                    or _rss_text(n, "description") or _rss_text(n, "content")
                    or _rss_text(n, "summary"))
        summary = _clean_html(raw_body)
        items.append(NewsItem(
            title=title,
            url=_rss_text(n, "link") or "",
            published_at=_rss_text(n, "pubDate") or _rss_text(n, "published")
            or _rss_text(n, "updated") or "",
            source=source,
            kind="news",
            instruments=tag_instruments(f"{title} {summary}", symbols),
            summary=summary,
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


_REDDIT_UA = "Mozilla/5.0 (TempestDesk research; keyless public-json read)"


def parse_reddit(payload: dict, subreddit: str, symbols: list[str]) -> list[SocialPost]:
    """Parse reddit's public listing JSON ({data:{children:[{data:{title,selftext,score,...}}]}})
    into SocialPosts, tagging instruments from title + self-text. Returns [] on any shape error."""
    try:
        children = (payload or {}).get("data", {}).get("children", [])
    except (AttributeError, TypeError):
        return []
    out: list[SocialPost] = []
    for ch in children:
        d = ch.get("data", {}) if isinstance(ch, dict) else {}
        title = (d.get("title") or "").strip()
        if not title:
            continue
        body = _clean_html(d.get("selftext") or "")
        out.append(SocialPost(
            title=title, summary=body,
            score=int(d.get("score") or 0), num_comments=int(d.get("num_comments") or 0),
            source=subreddit, instruments=tag_instruments(f"{title} {body}", symbols)))
    return out


def _posts_for_sub(client, sub: str, symbols: list[str], per_sub: int,
                   state: dict | None = None) -> list[SocialPost]:
    """One subreddit's posts. Tries /hot.json first (richer — carries upvote `score`), which reddit
    OFTEN 403s for keyless/datacenter reads; falls back to the /.rss Atom feed (works keyless but
    has no score). Returns [] if both fail."""
    # cy323: reddit 403s keyless /hot.json for this IP (established cy320), so every attempt is a
    # wasted request out of a SHARED rate-limit bucket — and at 2 requests per sub that doubled cost
    # is what starved the second configured subreddit. Once json has failed within THIS call, skip
    # it for the remaining subs. Scoped per-call deliberately: a process-global made behaviour
    # order-dependent across the test suite, which is a smell about the design, not just the tests.
    st = state if state is not None else {}
    if not st.get("json_dead"):
        try:
            r = client.get(f"https://www.reddit.com/r/{sub}/hot.json",
                           params={"limit": per_sub}, headers={"User-Agent": _REDDIT_UA})
            r.raise_for_status()
            posts = parse_reddit(r.json(), subreddit=sub, symbols=symbols)
            if posts:
                return posts[:per_sub]
            st["json_dead"] = True
        except Exception:
            st["json_dead"] = True
        except Exception:
            _reddit_json_failures += 1
    try:
        r = client.get(f"https://www.reddit.com/r/{sub}/.rss", headers={"User-Agent": _REDDIT_UA})
        r.raise_for_status()
        return [SocialPost(title=i.title, summary=i.summary, source=sub,
                           instruments=i.instruments, source_kind="rss")
                for i in parse_rss(r.content, source=sub, symbols=symbols)[:per_sub]]
    except Exception:
        return []


_REDDIT_PAUSE_SECONDS = 2.5


def fetch_reddit(client, subreddits: list[str], symbols: list[str], per_sub: int = 40,
                 sleep=None, pause_seconds: float = _REDDIT_PAUSE_SECONDS) -> dict:
    """Keyless reddit social-sentiment scrape. Aggregates the top posts and a per-symbol mention
    count + score-weighted sum (the crowd's attention/weight per coin), so the Sentiment analyst
    reads real crowd CONTENT, not just a Fear&Greed number. Per sub it tries /hot.json then falls
    back to the /.rss Atom feed (reddit 403s the keyless JSON but serves the RSS). Graceful: a
    blocked sub is skipped; if all fail, returns empty and the desk caps conviction (the persona
    handles the degraded read).

    PACING (cy323): each sub costs up to TWO back-to-back requests (json, then the rss fallback),
    and reddit 429s almost everything after the first. With no delay the SECOND configured
    subreddit was silently lost every cycle — verified live on the production path, where
    `fetch_reddit(['CryptoCurrency','CryptoMarkets'])` returned 25 posts ALL from CryptoCurrency
    and ZERO from CryptoMarkets, while the exception handler swallowed the 429 into []. That is
    not a redundant source: fetched on its own, CryptoMarkets returned a HYPE mention, a symbol
    the Sentiment analyst had reported zero coverage on for SEVEN consecutive cycles while
    correctly complaining the feed only ever showed BTC/ETH. A configured source that never loads
    is the d6da6f70 silent-off-switch pattern. We now pause between subs, and report any that
    yielded nothing via `empty_subreddits` so a silent loss becomes a visible one."""
    if sleep is None:
        import time as _time
        sleep = _time.sleep
    seen: set[str] = set()
    posts: list[SocialPost] = []
    empty: list[str] = []
    _state: dict = {}          # per-call: "has the json path already failed this run?"
    for i, sub in enumerate(subreddits):
        if i:                      # pace only BETWEEN subs — never before the first
            sleep(pause_seconds)
        got = _posts_for_sub(client, sub, symbols, per_sub, state=_state)
        if not got:
            empty.append(sub)
        for p in got:
            if p.title not in seen:
                seen.add(p.title)
                posts.append(p)
    posts.sort(key=lambda p: p.score, reverse=True)
    mentions: dict[str, dict] = {}
    for p in posts:
        for sym in p.instruments:
            m = mentions.setdefault(sym, {"count": 0, "score_sum": 0})
            m["count"] += 1
            m["score_sum"] += p.score
    return {"posts": [p.model_dump() for p in posts[:30]], "mentions": mentions,
            "empty_subreddits": empty}


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

# Futures-Fund — Data Feeds Integration (Fix) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Actually wire the market-data feeds into the cycle so they reach the agents. Root cause (diagnosed): the feeds are implemented + unit-tested but have **zero call sites** in the live cycle — `build_symbol_brief` only uses `ohlcv`+`funding`. Fix: replace the dead CryptoPanic news feed with a **keyless RSS news source**, wire **FRED macro** (key now provided), enrich the brief with **OI + long/short**, add a **market-context** block (news + Fear&Greed + macro) to preflight, self-archive the 30-day-limited data, and **degrade gracefully** when a feed/key is unavailable.

**Verified live (this session):** FRED works with the supplied key (DGS10/DTWEXBGS/FEDFUNDS/CPIAUCSL all 200); keyless RSS (CoinDesk, CoinTelegraph) returns real, parseable, symbol-relevant headlines; Fear&Greed (200) + OI + long/short are keyless and work. CryptoPanic is **scrapped** (no more free keys; 404 anonymously).

**Tech Stack:** Python 3.11 / uv, pydantic v2, httpx, stdlib `xml.etree` for RSS, pytest, ruff. Tests are offline (fixture RSS/JSON + a fake HTTP client + fake exchange).

**Design / decisions:**
- **News = keyless RSS** from configurable sources (default CoinDesk + CoinTelegraph). Headlines are tagged with the symbols they mention. CryptoPanic removed entirely.
- **Macro = FRED** (key from env `FRED_API_KEY`, stored in gitignored `.env`); graceful if absent.
- **Graceful degradation:** any feed that fails → omitted + a `warnings` entry; the news/sentiment/macro analysts then **cap conviction** (mission §5).
- All injected (http client / exchange) so tests stay offline.

---

## File Structure

```
futures_fund/
  vendors.py         # (edit) remove CryptoPanic; add parse_rss + fetch_news (+ tag) + fetch_macro
  config.py          # (edit) drop cryptopanic_*; add news_rss_sources
  brief.py           # (edit) enrich build_symbol_brief with OI + long/short (graceful)
  market_context.py  # (new) build_market_context: news + fear_greed + macro + warnings
  orchestration.py   # (edit) preflight_step builds + attaches market_context; self-archive OI/LS
agents/
  news.md sentiment.md derivatives.md   # (edit) Inputs now reference the real feed data
SKILL.md             # (edit) note market_context is injected to news/sentiment/macro agents
tests/
  test_vendors.py · test_brief.py · test_market_context.py · test_orchestration.py  # (edit/add)
```

---

## Task 1: Keyless RSS news + FRED macro in `vendors.py` (drop CryptoPanic)

**Files:** modify `futures_fund/vendors.py`, `tests/test_vendors.py`.

- [ ] **Step 1: Edit the failing test** — in `tests/test_vendors.py`: DELETE the CryptoPanic test (`test_parse_cryptopanic_v2_uses_instruments`) and any `parse_cryptopanic`/`fetch_cryptopanic` import. ADD these tests:

```python
from futures_fund.vendors import NewsItem, fetch_macro, fetch_news, parse_rss, tag_instruments

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
```

- [ ] **Step 2: Run** `uv run pytest tests/test_vendors.py -v` — expect FAIL.

- [ ] **Step 3: Edit `futures_fund/vendors.py`:**
  - DELETE `CRYPTOPANIC_URL`, `parse_cryptopanic`, and `fetch_cryptopanic`.
  - Keep `FNG_URL`, `FRED_URL`, `FearGreed`, `NewsItem`, `parse_fear_greed`, `fetch_fear_greed`, `parse_fred`, `fetch_fred_series`, `archive_jsonl`.
  - ADD (anywhere after `NewsItem`): the imports `import xml.etree.ElementTree as ET` (top) and:

```python
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


def fetch_news(client, sources: list[str], symbols: list[str], per_source: int = 10) -> list[NewsItem]:
    """Fetch + parse multiple keyless RSS news feeds; skip any source that errors; dedupe by title."""
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
                                              "file_type": "json", "sort_order": "desc", "limit": 1})
            r.raise_for_status()
            vals = parse_fred(r.json())  # [(date, value)], skips "."
            if vals:
                out[sid] = max(vals, key=lambda dv: dv[0])[1]  # latest by ISO date — order-independent
        except Exception:
            continue
    return out
```

- [ ] **Step 4: Run** `uv run pytest tests/test_vendors.py -v` — expect PASS (the F&G/FRED/archive tests + the new RSS/macro/tag tests; report count). Then `uv run ruff check futures_fund/vendors.py tests/test_vendors.py`.

- [ ] **Step 5: Commit**

```bash
git add futures_fund/vendors.py tests/test_vendors.py
git commit -m "feat: keyless RSS news + FRED macro fetchers; drop dead CryptoPanic feed"
```

---

## Task 2: Config — drop CryptoPanic, add RSS sources

**Files:** modify `futures_fund/config.py`, `tests/test_config.py`.

- [ ] **Step 1: Edit `futures_fund/config.py` `DataSettings`:** remove `cryptopanic_token_env` and the `cryptopanic_token` property. Add a sources field (keep `fred_*`):

```python
    news_rss_sources: list[str] = Field(default_factory=lambda: [
        "https://www.coindesk.com/arc/outboundfeeds/rss/?outputType=xml",
        "https://cointelegraph.com/rss",
    ])
```

- [ ] **Step 2: Edit `tests/test_config.py`:** in `test_missing_secret_is_none`, replace the CryptoPanic assertion. Update it to:

```python
def test_missing_secret_is_none(monkeypatch):
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    s = Settings()
    assert s.data.fred_api_key is None


def test_news_sources_default_present():
    s = Settings()
    assert any("coindesk" in u for u in s.data.news_rss_sources)
    assert any("cointelegraph" in u for u in s.data.news_rss_sources)
```

- [ ] **Step 3: Run** `uv run pytest tests/test_config.py -v` — expect PASS. `uv run ruff check futures_fund/config.py tests/test_config.py`.

- [ ] **Step 4: Update `config.yaml` + `.env.example`:** in `config.yaml` under `data:` remove `cryptopanic_token_env` and add `news_rss_sources` (the two default URLs). In `.env.example` remove the `CRYPTOPANIC_TOKEN=` line (keep `FRED_API_KEY=`).

- [ ] **Step 5: Commit**

```bash
git add futures_fund/config.py tests/test_config.py config.yaml .env.example
git commit -m "feat: config drops CryptoPanic, adds keyless news_rss_sources"
```

---

## Task 3: Enrich the brief with OI + long/short

**Files:** modify `futures_fund/brief.py`, `tests/test_brief.py`.

- [ ] **Step 1: Edit `tests/test_brief.py`:** extend the `FakeExchange` to provide `open_interest_history` and `long_short_ratio`, and assert the brief carries them:

```python
import pandas as pd  # (ensure imported)

# add to FakeExchange:
    def open_interest_history(self, symbol, period="4h", limit=200):
        return pd.DataFrame({"timestamp": pd.date_range("2026-01-01", periods=3, freq="4h", tz="UTC"),
                             "oi_amount": [100.0, 101.0, 99.0], "oi_value": [1.0e7, 1.01e7, 0.99e7]})

    def long_short_ratio(self, symbol, period="4h", limit=200):
        return pd.DataFrame({"timestamp": pd.date_range("2026-01-01", periods=2, freq="4h", tz="UTC"),
                             "long_short_ratio": [1.5, 1.6], "long_account": [0.6, 0.62],
                             "short_account": [0.4, 0.38]})


def test_brief_includes_derivatives_signals():
    b = build_symbol_brief(FakeExchange(_uptrend()), "BTC/USDT:USDT")
    assert b["long_short_ratio"] == 1.6 and b["long_account"] == 0.62
    assert "oi_value" in b and b["oi_value"] > 0
    assert "oi_change" in b


def test_brief_degrades_when_derivatives_unavailable():
    class NoDeriv(FakeExchange):
        def open_interest_history(self, *a, **k):
            raise RuntimeError("unavailable")
        def long_short_ratio(self, *a, **k):
            raise RuntimeError("unavailable")
    b = build_symbol_brief(NoDeriv(_uptrend()), "BTC/USDT:USDT")
    assert b["long_short_ratio"] is None and b["oi_value"] is None
```

(The existing `test_brief_has_expected_keys_and_types` / `test_brief_momentum_positive_on_uptrend` still pass — but their `FakeExchange` now needs the two new methods; add them to that fake too.)

- [ ] **Step 2: Run** `uv run pytest tests/test_brief.py -v` — expect FAIL.

- [ ] **Step 3: Edit `futures_fund/brief.py`** — append derivatives signals (graceful):

```python
def _derivatives(exchange, symbol: str, timeframe: str) -> dict:
    """OI trend + long/short positioning; all-None if the feed is unavailable (graceful)."""
    out = {"oi_value": None, "oi_change": None, "long_short_ratio": None, "long_account": None}
    try:
        oi = exchange.open_interest_history(symbol, period=timeframe, limit=12)
        if len(oi) > 1:
            out["oi_value"] = float(oi["oi_value"].iloc[-1])
            base = oi["oi_value"].iloc[0]
            out["oi_change"] = float(oi["oi_value"].iloc[-1] / base - 1.0) if base else 0.0
    except Exception:
        pass
    try:
        lsr = exchange.long_short_ratio(symbol, period=timeframe, limit=6)
        if len(lsr):
            out["long_short_ratio"] = float(lsr["long_short_ratio"].iloc[-1])
            out["long_account"] = float(lsr["long_account"].iloc[-1])
    except Exception:
        pass
    return out
```
and in `build_symbol_brief`, before the `return`, merge it: change the return dict to include `**_derivatives(exchange, symbol, timeframe)` (add that line inside the returned dict literal, e.g. after `"mark_price": ...,`).

- [ ] **Step 4: Run** `uv run pytest tests/test_brief.py -v` — expect PASS. `uv run ruff check futures_fund/brief.py tests/test_brief.py`.

- [ ] **Step 5: Commit**

```bash
git add futures_fund/brief.py tests/test_brief.py
git commit -m "feat: enrich symbol brief with OI trend + long/short positioning (graceful)"
```

---

## Task 4: The market-context builder

**Files:** create `futures_fund/market_context.py`, `tests/test_market_context.py`.

- [ ] **Step 1: Write the failing test** — `tests/test_market_context.py`:

```python
from futures_fund.config import Settings
from futures_fund.market_context import build_market_context

_RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel>
<item><title>Bitcoin ETFs see record outflows</title><link>http://x/1</link>
<pubDate>Fri, 29 May 2026 14:20:32 +0000</pubDate></item></channel></rss>"""
_FNG = {"data": [{"value": "23", "value_classification": "Extreme Fear", "timestamp": "1780012800"}]}
_FRED = {"observations": [{"date": "2026-05-27", "value": "4.48"}]}


class _Resp:
    def __init__(self, *, content=b"", payload=None, status=200):
        self.content = content; self._p = payload; self.status_code = status
    def raise_for_status(self):
        if self.status_code >= 400: raise RuntimeError("http")
    def json(self): return self._p


class _Client:
    def __init__(self, fng_fail=False):
        self.fng_fail = fng_fail
    def get(self, url, params=None, **kw):
        if "alternative.me" in url:
            return _Resp(status=500) if self.fng_fail else _Resp(payload=_FNG)
        if "stlouisfed" in url:
            return _Resp(payload=_FRED)
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
    assert mc["warnings"] == []


def test_market_context_degrades_without_fred_key():
    mc = build_market_context(_Client(), _settings(), fred_key=None)
    assert mc["macro"] == {}
    assert any("macro" in w.lower() for w in mc["warnings"])


def test_market_context_degrades_when_fear_greed_down():
    mc = build_market_context(_Client(fng_fail=True), _settings(), fred_key="k" * 32)
    assert mc["fear_greed"] is None
    assert any("fear" in w.lower() or "sentiment" in w.lower() for w in mc["warnings"])
```

- [ ] **Step 2: Run** `uv run pytest tests/test_market_context.py -v` — expect FAIL.

- [ ] **Step 3: Implement** `futures_fund/market_context.py`:

```python
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
```

- [ ] **Step 4: Run** `uv run pytest tests/test_market_context.py -v` — expect PASS (3 passed). `uv run ruff check futures_fund/market_context.py tests/test_market_context.py`.

- [ ] **Step 5: Commit**

```bash
git add futures_fund/market_context.py tests/test_market_context.py
git commit -m "feat: market-context builder (news + Fear&Greed + macro) with graceful degradation"
```

---

## Task 5: Wire market-context + self-archiving into preflight

**Files:** modify `futures_fund/orchestration.py`, `tests/test_orchestration.py`.

- [ ] **Step 1: Edit `tests/test_orchestration.py`:** the preflight tests must now inject a fake HTTP client (so they stay offline) and assert the context carries `market_context`. Add a tiny fake client + extend the `FakeExchange` with `open_interest_history`/`long_short_ratio` (so the enriched brief works), then update the preflight test:

```python
# add near the top of the test module:
_RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel><item>
<title>BTC chops sideways</title><link>http://x/1</link>
<pubDate>Fri, 29 May 2026 14:20:32 +0000</pubDate></item></channel></rss>"""

class _Resp:
    def __init__(self, *, content=b"", payload=None, status=200):
        self.content=content; self._p=payload; self.status_code=status
    def raise_for_status(self):
        if self.status_code>=400: raise RuntimeError("http")
    def json(self): return self._p

class _HttpClient:
    def get(self, url, params=None, **kw):
        if "alternative.me" in url:
            return _Resp(payload={"data":[{"value":"30","value_classification":"Fear","timestamp":"1780012800"}]})
        return _Resp(content=_RSS)

# extend FakeExchange (in this file) with:
    def open_interest_history(self, symbol, period="4h", limit=200):
        import pandas as pd
        return pd.DataFrame({"timestamp": pd.date_range("2026-01-01", periods=3, freq="4h", tz="UTC"),
                             "oi_amount":[1.,1.,1.], "oi_value":[1e7,1e7,1e7]})
    def long_short_ratio(self, symbol, period="4h", limit=200):
        import pandas as pd
        return pd.DataFrame({"timestamp": pd.date_range("2026-01-01", periods=2, freq="4h", tz="UTC"),
                             "long_short_ratio":[1.5,1.6],"long_account":[0.6,0.62],"short_account":[0.4,0.38]})
```
And change the existing `test_preflight_emits_context_with_briefs` (and any other preflight call in this file) to pass `http_client=_HttpClient()`, then add:

```python
def test_preflight_attaches_market_context(tmp_path):
    ex = FakeExchange({"BTC/USDT:USDT": _uptrend()})
    ctx = preflight_step(ex, _settings(), tmp_path / "s", tmp_path / "m",
                         now=datetime(2026, 3, 1, tzinfo=UTC), cycle_no=1, http_client=_HttpClient())
    mc = ctx["market_context"]
    assert mc["fear_greed"]["value"] == 30
    assert isinstance(mc["news"], list)
    assert "warnings" in mc
    # the brief now carries derivatives positioning
    assert "long_short_ratio" in ctx["briefs"][0]
```

- [ ] **Step 2: Run** `uv run pytest tests/test_orchestration.py -v` — expect FAIL (no `http_client` param / no `market_context`).

- [ ] **Step 3: Edit `futures_fund/orchestration.py` `preflight_step`:** add a trailing `http_client=None` parameter; build the market context and attach it to BOTH return dicts; self-archive OI/long-short. Concretely:
  - Signature: `def preflight_step(exchange, settings, state_dir, memory_dir, now, cycle_no, http_client=None) -> dict:`
  - After `ensure_memory_layout(...)` (top), add:
    ```python
    import os
    from futures_fund.market_context import build_market_context
    if http_client is None:
        import httpx
        http_client = httpx.Client(timeout=15.0)
    market_context = build_market_context(http_client, settings,
                                          fred_key=os.environ.get(settings.data.fred_key_env))
    ```
  - In the **halted** early-return dict, add `"market_context": market_context,`.
  - In the **normal** return dict, add `"market_context": market_context,`.
  - After building `briefs` (the non-halted path), self-archive the derivatives history:
    ```python
    from futures_fund.vendors import archive_jsonl
    for b in briefs:
        rec = {"ts": now.isoformat(), "symbol": b["exchange_id"],
               "oi_value": b.get("oi_value"), "long_short_ratio": b.get("long_short_ratio")}
        archive_jsonl(f"{settings.data.archive_dir}/derivatives.jsonl", [rec], key="ts")
    ```
    (archive_dir defaults to `state/archive`, gitignored.) Wrap in try/except so archiving never breaks the cycle.

- [ ] **Step 4: Run** `uv run pytest tests/test_orchestration.py -v` — expect PASS. `uv run ruff check futures_fund/orchestration.py tests/test_orchestration.py`.

- [ ] **Step 5: Commit**

```bash
git add futures_fund/orchestration.py tests/test_orchestration.py
git commit -m "feat: preflight builds + attaches market_context (news/sentiment/macro) + self-archives derivatives"
```

---

## Task 6: Update role files + SKILL.md + scripts/preflight CLI + full suite

**Files:** modify `agents/news.md`, `agents/sentiment.md`, `agents/derivatives.md`, `SKILL.md`, `scripts/preflight.py`.

- [ ] **Step 1: `scripts/preflight.py`** already calls `preflight_step` then saves context — no change needed for the http client (preflight builds a real one when `http_client=None`). Verify it passes no `http_client` (so production fetches live). (If it currently passes extra args, leave them.)

- [ ] **Step 2: Edit the three analyst role files' `## Inputs`** to reference the real feed data (they previously had no live feed):
  **Sharpened, NON-OVERLAPPING lanes (decided): News = discrete events; Sentiment/Macro = the ambient backdrop; positioning lives with Derivatives. Each role file must state its lane AND what it must NOT do.**
  - `agents/news.md`: **The event desk — discrete, datable CATALYSTS ONLY.** Inputs: `market_context.news` (recent headlines, each with title/url/source/published_at + the `instruments` symbols it mentions). Guidance: identify discrete catalysts (ETF flows, hacks/exploits, regulatory/legal rulings, listings/delistings, protocol upgrades, exchange events) and their directional lean; set `risk_off_flag=1` on a clear adverse catalyst. **Do NOT opine on crowd mood/Fear&Greed (that's Sentiment) or futures positioning (that's Derivatives).** If `market_context.warnings` flags the news feed unavailable OR there is no datable catalyst, return `stance:neutral` with low confidence and say so (no fabricated catalysts).
  - `agents/sentiment.md`: **The backdrop desk — ambient MOOD + MACRO ONLY.** Inputs: `market_context.fear_greed` (value + classification) and `market_context.macro` (DTWEXBGS=broad dollar, DGS10=10y yield, FEDFUNDS, CPIAUCSL). Guidance: Fear&Greed is CONTRARIAN at extremes; read DXY + 10y yields + Fed funds for the risk-on/off regime; de-risk into hot CPI/FOMC. **Do NOT react to individual headlines (that's News) and do NOT read long/short positioning (that's Derivatives).** If macro or Fear&Greed is in `warnings`, cap conviction and note the missing read.
  - `agents/derivatives.md`: **Owns POSITIONING & flow.** Inputs: the per-symbol brief now carries `funding_rate`, `oi_value`, `oi_change`, `long_short_ratio`, `long_account`. Reason about crowding/squeeze risk, funding carry, OI behavior, liquidation fuel. If those fields are `null`, say the derivatives feed is degraded and cap conviction.

- [ ] **Step 3: Edit `SKILL.md`** — in `## Subagent dispatch rules`, add a bullet: the cycle context (`state/cycle/N/context.json`) now carries a `market_context` block (news headlines, Fear&Greed, macro) and the per-symbol briefs carry OI/long-short. Inject the relevant slice into each analyst (news→`market_context.news`, sentiment→`market_context.fear_greed`+`.macro`, derivatives→brief positioning); honor `market_context.warnings` by capping conviction on any degraded feed.

- [ ] **Step 4: Run the FULL suite + lint** `uv run pytest` then `uv run ruff check .`. Report the EXACT total. (Net test change: vendors -1 cryptopanic +6 news/macro; config +1; brief +2; market_context +3; orchestration +1 → roughly +12 from the pre-fix count.)

- [ ] **Step 5: Commit**

```bash
git add agents/news.md agents/sentiment.md agents/derivatives.md SKILL.md scripts/preflight.py
git commit -m "feat: analyst role files + SKILL.md consume the wired feeds (news/sentiment/macro/derivatives)"
```

---

## Self-Review (completed during planning)

**Root cause addressed:** the feeds now have live call sites — `build_symbol_brief` pulls OI/long-short; `preflight_step` builds `market_context` (RSS news + Fear&Greed + FRED macro) and attaches it to the context every agent sees; `archive_jsonl` self-archives the 30-day-limited derivatives data. CryptoPanic is removed (dead). FRED is wired (key in gitignored `.env`).

**Verified dependencies (live, this session):** FRED+key → 200; CoinDesk/CoinTelegraph RSS → real headlines; Fear&Greed → 200; OI/long-short → keyless OK. The fix is built on confirmed-working feeds, not assumptions.

**Graceful degradation (mission §5):** every feed fails independently → omitted + a `warnings` entry → the role files instruct the affected analyst to cap conviction. No feed failure can break the cycle (all wrapped). Tests cover the no-FRED-key, dead-source, and feed-down paths.

**Offline tests:** all new tests inject a fake HTTP client / fake exchange + fixture RSS/JSON — no network. The existing preflight tests are updated to inject the fake client.

**Type consistency:** `fetch_news`→`NewsItem` (existing model, votes default 0); `fetch_macro`→`dict[str,float]`; `build_market_context`→ a JSON-serializable dict saved into `context.json`; brief gains `oi_value/oi_change/long_short_ratio/long_account` (None when degraded). `preflight_step` gains an optional `http_client` (default real httpx) so production fetches live and tests inject a fake.

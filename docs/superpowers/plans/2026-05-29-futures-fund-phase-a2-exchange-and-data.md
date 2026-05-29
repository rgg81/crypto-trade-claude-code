# Futures-Fund Phase A2 — Exchange & Data Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the data layer that feeds the A1 risk core: a typed config loader, a ccxt-based Binance USD-M (testnet-capable) exchange client, free-tier data vendors (Fear&Greed, CryptoPanic, FRED) with self-archiving, and the three vendored analytical skills — all unit-tested **offline** (no network in the test suite).

**Architecture:** Separate **pure parsers** (raw API dict → our domain types / DataFrames) from **thin I/O wrappers** (build the ccxt/httpx client, call it, hand the response to a parser). Unit tests exercise the parsers against captured fixture payloads and the wrappers against injected fakes, so the suite is deterministic and network-free. A standalone, opt-in `scripts/smoke_testnet.py` is the only thing that touches the live testnet (run manually, never in CI).

**Tech Stack:** Python 3.11 / uv, pydantic v2, pandas/numpy, **ccxt 4.5+** (Binance USD-M), **httpx** (vendor HTTP), **scipy** (needed by vendored overfit/feature scripts), **pyyaml** (config), pytest, ruff.

**Reference:** spec `docs/superpowers/specs/2026-05-29-futures-fund-design.md` (§10 data sources, §7 cost/risk that consumes this); A1 is merged on `main` (`futures_fund/models.py` has `SymbolSpec`, `MmrBracket`).

**Verified API facts this plan relies on (ccxt 4.5.56, live-checked):**
- `ccxt.binanceusdm(...)`; `set_sandbox_mode(True)` → testnet `https://testnet.binancefuture.com`. Unified symbol `BTC/USDT:USDT`, raw id at `market['id']` = `BTCUSDT`.
- `precisionMode == TICK_SIZE`: `market['precision']['price']` **is** the tick size, `['amount']` **is** the step size (floats). `market['limits']['cost']['min']` = min notional.
- `fetch_funding_rate(s)` → `{'fundingRate'(current), 'fundingTimestamp'(ms, next), 'markPrice', 'indexPrice', ...}` (no predicted next rate). `fetch_funding_interval(s)` → `{'interval':'8h','info':{'fundingIntervalHours':8}}`; **symbols absent from fundingInfo default to 8h**.
- `fetch_ohlcv(s, tf, since, limit≤1500)` → `[[ts_ms,o,h,l,c,v], ...]` ascending.
- `fetch_open_interest_history(s, period, since, limit≤500)` → items with `openInterestAmount`, `openInterestValue`; periods 5m..1d (not 1m); ~30-day retention.
- Long/short ratio = **implicit** raw method `fapiDataGetGlobalLongShortAccountRatio({'symbol':'BTCUSDT','period':'4h','limit':N})` → list of `{'symbol','longShortRatio','longAccount','shortAccount','timestamp'}` (string numbers).
- `fetch_leverage_tiers([s])` → `{sym:[{'minNotional','maxNotional','maintenanceMarginRate','maxLeverage','info':{'cum':'...'}}]}`. **PRIVATE — needs API keys.** `info['cum']` = maintenance amount.
- F&G `GET https://api.alternative.me/fng/?limit=N&format=json` → `{'data':[{'value'(str),'value_classification','timestamp'(str)}]}` (no key).
- CryptoPanic v2 `GET https://cryptopanic.com/api/developer/v2/posts/?auth_token=..&public=true&currencies=BTC,ETH&kind=news` → `{'next','results':[{'title','url','published_at','kind','source':{'title'},'instruments':[{'code'}],'votes':{'positive','negative'}}]}`.
- FRED `GET https://api.stlouisfed.org/fred/series/observations?series_id=..&api_key=..&file_type=json` → `{'observations':[{'date','value'(str, '.'=missing)}]}`. Series: DTWEXBGS, DGS10 (daily), FEDFUNDS, CPIAUCSL (monthly).

---

## File Structure

```
futures_fund/
  config.py            # typed Settings loaded from config.yaml + env-var secrets
  market_data.py       # FundingInfo DTO + PURE parsers (raw ccxt dicts -> domain types / DataFrames)
  exchange.py          # build_ccxt() + FuturesExchange thin wrapper (calls ccxt, delegates to parsers)
  vendors.py           # FearGreed/NewsItem DTOs + pure parsers + httpx fetchers + archive_jsonl
  vendor/              # VENDORED analytical skills (copied verbatim, provenance-tracked)
    __init__.py
    PROVENANCE.md
    regime_detection.py
    feature_engineering.py
    walk_forward.py
    overfit_detector.py
config.yaml            # non-secret config (committed)
.env.example           # secret env var names (committed; real .env gitignored)
scripts/
  smoke_testnet.py     # MANUAL opt-in live testnet check (not run by pytest)
tests/
  test_config.py
  test_market_data.py
  test_exchange.py
  test_vendors.py
  test_vendor_skills.py
```

---

## Task 1: Dependencies + typed config

**Files:** modify `pyproject.toml`; create `futures_fund/config.py`, `config.yaml`, `.env.example`, `tests/test_config.py`.

- [ ] **Step 1: Add dependencies** — edit `pyproject.toml` `[project].dependencies` to add `ccxt>=4.5`, `httpx>=0.27`, `scipy>=1.11`, `pyyaml>=6.0` (keep existing pydantic/numpy/pandas). Then run `uv sync` (expect exit 0).

- [ ] **Step 2: Write the failing test** — `tests/test_config.py`:

```python
from pathlib import Path

from futures_fund.config import Settings, load_settings


def test_defaults_when_no_file(tmp_path):
    s = load_settings(tmp_path / "missing.yaml")
    assert s.account_size_usdt == 10_000.0
    assert s.timeframe == "4h"
    assert s.symbol_count == 10
    assert s.exchange.testnet is True
    assert s.data.fred_series == ["DTWEXBGS", "DGS10", "FEDFUNDS", "CPIAUCSL"]


def test_yaml_overrides(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("account_size_usdt: 25000\nsymbol_count: 5\nexchange:\n  testnet: false\n")
    s = load_settings(p)
    assert s.account_size_usdt == 25000.0
    assert s.symbol_count == 5
    assert s.exchange.testnet is False


def test_secrets_read_from_env(monkeypatch):
    monkeypatch.setenv("BINANCE_KEY", "abc")
    monkeypatch.setenv("BINANCE_SECRET", "xyz")
    s = Settings()
    assert s.exchange.api_key == "abc"
    assert s.exchange.api_secret == "xyz"


def test_missing_secret_is_none(monkeypatch):
    monkeypatch.delenv("CRYPTOPANIC_TOKEN", raising=False)
    s = Settings()
    assert s.data.cryptopanic_token is None
```

- [ ] **Step 3: Run** `uv run pytest tests/test_config.py -v` — expect FAIL (no config module).

- [ ] **Step 4: Implement** `futures_fund/config.py`:

```python
from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class ExchangeSettings(BaseModel):
    testnet: bool = True
    key_env: str = "BINANCE_KEY"
    secret_env: str = "BINANCE_SECRET"

    @property
    def api_key(self) -> str | None:
        return os.environ.get(self.key_env)

    @property
    def api_secret(self) -> str | None:
        return os.environ.get(self.secret_env)


class DataSettings(BaseModel):
    cryptopanic_token_env: str = "CRYPTOPANIC_TOKEN"
    fred_key_env: str = "FRED_API_KEY"
    fred_series: list[str] = Field(default_factory=lambda: ["DTWEXBGS", "DGS10", "FEDFUNDS", "CPIAUCSL"])
    archive_dir: str = "state/archive"

    @property
    def cryptopanic_token(self) -> str | None:
        return os.environ.get(self.cryptopanic_token_env)

    @property
    def fred_api_key(self) -> str | None:
        return os.environ.get(self.fred_key_env)


class Settings(BaseModel):
    account_size_usdt: float = 10_000.0
    timeframe: str = "4h"
    symbol_count: int = 10
    deep_model: str = "opus"
    quick_model: str = "haiku"
    verdict_horizon_weeks: int = 8
    exchange: ExchangeSettings = Field(default_factory=ExchangeSettings)
    data: DataSettings = Field(default_factory=DataSettings)


def load_settings(path: str | Path = "config.yaml") -> Settings:
    """Load non-secret config from YAML (defaults if file absent). Secrets come from env."""
    p = Path(path)
    raw = yaml.safe_load(p.read_text()) if p.exists() else {}
    return Settings(**(raw or {}))
```

- [ ] **Step 5: Create `config.yaml`** (committed, non-secret):

```yaml
# Operation TEMPEST — non-secret runtime config. Secrets come from env (.env), never here.
account_size_usdt: 10000.0
timeframe: "4h"
symbol_count: 10
deep_model: "opus"
quick_model: "haiku"
verdict_horizon_weeks: 8
exchange:
  testnet: true
  key_env: "BINANCE_KEY"
  secret_env: "BINANCE_SECRET"
data:
  cryptopanic_token_env: "CRYPTOPANIC_TOKEN"
  fred_key_env: "FRED_API_KEY"
  fred_series: ["DTWEXBGS", "DGS10", "FEDFUNDS", "CPIAUCSL"]
  archive_dir: "state/archive"
```

- [ ] **Step 6: Create `.env.example`** (committed; real `.env` is gitignored):

```
# Binance USD-M testnet keys (testnet.binancefuture.com). Leave blank for public-data-only.
BINANCE_KEY=
BINANCE_SECRET=
# Free data vendor keys
CRYPTOPANIC_TOKEN=
FRED_API_KEY=
```

- [ ] **Step 7: Run** `uv run pytest tests/test_config.py -v` — expect PASS (4 passed). Then `uv run ruff check futures_fund/config.py tests/test_config.py config.yaml` (yaml is skipped by ruff; fix any py style).

- [ ] **Step 8: Commit**

```bash
git add pyproject.toml uv.lock futures_fund/config.py config.yaml .env.example tests/test_config.py
git commit -m "feat: deps (ccxt/httpx/scipy/pyyaml) + typed config loader"
```

---

## Task 2: Market-data DTO + pure parsers

**Files:** create `futures_fund/market_data.py`, `tests/test_market_data.py`.

- [ ] **Step 1: Write the failing test** — `tests/test_market_data.py` (fixtures mirror the verified ccxt shapes):

```python
import pandas as pd

from futures_fund.market_data import (
    FundingInfo,
    parse_funding,
    parse_long_short_ratio,
    parse_ohlcv,
    parse_open_interest_history,
    parse_symbol_spec,
)

MARKET = {
    "id": "BTCUSDT",
    "symbol": "BTC/USDT:USDT",
    "precision": {"price": 0.1, "amount": 0.001},
    "limits": {"amount": {"min": 0.001, "max": 1000.0}, "cost": {"min": 100.0}},
    "contractSize": 1.0,
    "info": {"filters": []},
}
TIERS = [
    {"tier": 1, "minNotional": 0, "maxNotional": 50000, "maintenanceMarginRate": 0.004,
     "maxLeverage": 125, "info": {"cum": "0"}},
    {"tier": 2, "minNotional": 50000, "maxNotional": 250000, "maintenanceMarginRate": 0.005,
     "maxLeverage": 100, "info": {"cum": "50"}},
]


def test_parse_symbol_spec_maps_precision_and_brackets():
    spec = parse_symbol_spec(MARKET, TIERS)
    assert spec.symbol == "BTCUSDT"
    assert spec.tick_size == 0.1
    assert spec.step_size == 0.001
    assert spec.min_notional == 100.0
    assert len(spec.mmr_brackets) == 2
    b1 = spec.mmr_brackets[1]
    assert (b1.notional_floor, b1.notional_cap, b1.mmr, b1.maint_amount, b1.max_leverage) == \
        (50000.0, 250000.0, 0.005, 50.0, 100.0)


def test_parse_ohlcv_to_sorted_utc_dataframe():
    rows = [[1780000000000, 100.0, 105.0, 99.0, 104.0, 12.0],
            [1779996400000, 98.0, 101.0, 97.0, 100.0, 8.0]]  # out of order on purpose
    df = parse_ohlcv(rows)
    assert list(df.columns) == ["timestamp", "open", "high", "low", "close", "volume"]
    assert len(df) == 2
    assert df["timestamp"].is_monotonic_increasing
    assert str(df["timestamp"].dt.tz) == "UTC"
    assert df.iloc[-1]["close"] == 104.0


def test_parse_funding_uses_interval_or_defaults_8h():
    fr = {"symbol": "BTC/USDT:USDT", "fundingRate": 0.0001, "fundingTimestamp": 1780041600000,
          "markPrice": 73676.1, "indexPrice": 73702.25}
    fi = parse_funding(fr, {"interval": "4h", "info": {"fundingIntervalHours": 4}})
    assert isinstance(fi, FundingInfo)
    assert fi.current_rate == 0.0001
    assert fi.interval_hours == 4.0
    assert fi.mark_price == 73676.1
    assert str(fi.next_funding_ts.tzinfo) == "UTC"
    # absent interval -> default 8h
    assert parse_funding(fr, None).interval_hours == 8.0


def test_parse_open_interest_history():
    rows = [{"timestamp": 1780000000000, "openInterestAmount": 1234.5, "openInterestValue": 9.0e7},
            {"timestamp": 1779996400000, "openInterestAmount": 1200.0, "openInterestValue": 8.7e7}]
    df = parse_open_interest_history(rows)
    assert list(df.columns) == ["timestamp", "oi_amount", "oi_value"]
    assert df["timestamp"].is_monotonic_increasing
    assert df.iloc[-1]["oi_amount"] == 1234.5


def test_parse_long_short_ratio_casts_strings():
    raw = [{"symbol": "BTCUSDT", "longShortRatio": "1.5", "longAccount": "0.6",
            "shortAccount": "0.4", "timestamp": "1780000000000"}]
    df = parse_long_short_ratio(raw)
    assert df.iloc[0]["long_short_ratio"] == 1.5
    assert df.iloc[0]["long_account"] == 0.6


def test_parse_open_interest_empty_returns_empty_df():
    df = parse_open_interest_history([])
    assert df.empty
```

- [ ] **Step 2: Run** `uv run pytest tests/test_market_data.py -v` — expect FAIL.

- [ ] **Step 3: Implement** `futures_fund/market_data.py`:

```python
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
from pydantic import BaseModel

from futures_fund.models import MmrBracket, SymbolSpec


class FundingInfo(BaseModel):
    symbol: str
    current_rate: float       # ccxt fundingRate = Binance lastFundingRate (current, not predicted)
    next_funding_ts: datetime
    interval_hours: float
    mark_price: float
    index_price: float


def parse_symbol_spec(market: dict, tiers: list[dict]) -> SymbolSpec:
    """ccxt market dict + leverage tiers -> SymbolSpec. precisionMode is TICK_SIZE so
    precision.price/amount ARE the tick/step sizes."""
    brackets = [
        MmrBracket(
            notional_floor=float(t["minNotional"]),
            notional_cap=float(t["maxNotional"]),
            mmr=float(t["maintenanceMarginRate"]),
            maint_amount=float(t["info"]["cum"]),
            max_leverage=float(t["maxLeverage"]),
        )
        for t in tiers
    ]
    return SymbolSpec(
        symbol=market["id"],
        tick_size=float(market["precision"]["price"]),
        step_size=float(market["precision"]["amount"]),
        min_notional=float(market["limits"]["cost"]["min"]),
        mmr_brackets=brackets,
    )


def parse_ohlcv(rows: list[list]) -> pd.DataFrame:
    """ccxt OHLCV rows [[ts_ms,o,h,l,c,v], ...] -> sorted UTC-timestamped DataFrame."""
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return (
        df[["timestamp", "open", "high", "low", "close", "volume"]]
        .sort_values("timestamp")
        .reset_index(drop=True)
    )


def parse_funding(fr: dict, interval: dict | None = None) -> FundingInfo:
    interval_hours = 8.0
    if interval and (interval.get("info") or {}).get("fundingIntervalHours") is not None:
        interval_hours = float(interval["info"]["fundingIntervalHours"])
    return FundingInfo(
        symbol=fr["symbol"],
        current_rate=float(fr["fundingRate"]),
        next_funding_ts=datetime.fromtimestamp(fr["fundingTimestamp"] / 1000, tz=timezone.utc),
        interval_hours=interval_hours,
        mark_price=float(fr["markPrice"]),
        index_price=float(fr["indexPrice"]),
    )


def parse_open_interest_history(rows: list[dict]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=["timestamp", "oi_amount", "oi_value"])
    recs = [
        {
            "timestamp": pd.to_datetime(int(r["timestamp"]), unit="ms", utc=True),
            "oi_amount": float(r["openInterestAmount"]),
            "oi_value": float(r["openInterestValue"]) if r.get("openInterestValue") is not None else float("nan"),
        }
        for r in rows
    ]
    return pd.DataFrame(recs).sort_values("timestamp").reset_index(drop=True)


def parse_long_short_ratio(raw_rows: list[dict]) -> pd.DataFrame:
    if not raw_rows:
        return pd.DataFrame(columns=["timestamp", "long_short_ratio", "long_account", "short_account"])
    recs = [
        {
            "timestamp": pd.to_datetime(int(r["timestamp"]), unit="ms", utc=True),
            "long_short_ratio": float(r["longShortRatio"]),
            "long_account": float(r["longAccount"]),
            "short_account": float(r["shortAccount"]),
        }
        for r in raw_rows
    ]
    return pd.DataFrame(recs).sort_values("timestamp").reset_index(drop=True)
```

- [ ] **Step 4: Run** `uv run pytest tests/test_market_data.py -v` — expect PASS (6 passed). Then `uv run ruff check futures_fund/market_data.py tests/test_market_data.py` — fix style only.

- [ ] **Step 5: Commit**

```bash
git add futures_fund/market_data.py tests/test_market_data.py
git commit -m "feat: pure market-data parsers (symbol spec, ohlcv, funding, OI, long/short)"
```

---

## Task 3: Exchange client wrapper + live smoke script

**Files:** create `futures_fund/exchange.py`, `tests/test_exchange.py`, `scripts/smoke_testnet.py`.

The wrapper is tested with an **injected fake ccxt object** (no network). Parsing correctness is already covered by Task 2; here we verify wiring: the right ccxt method is called, the unified→raw symbol id conversion for implicit endpoints, and that results are the parsed domain types.

- [ ] **Step 1: Write the failing test** — `tests/test_exchange.py`:

```python
import pandas as pd

from futures_fund.exchange import FuturesExchange
from futures_fund.market_data import FundingInfo
from futures_fund.models import SymbolSpec


class FakeCcxt:
    """Minimal stand-in for a ccxt binanceusdm client."""

    def __init__(self):
        self.calls = []
        self.sandbox = False

    def set_sandbox_mode(self, on):
        self.sandbox = on

    def load_markets(self):
        self.calls.append("load_markets")

    def market(self, symbol):
        return {
            "id": "BTCUSDT", "symbol": symbol,
            "precision": {"price": 0.1, "amount": 0.001},
            "limits": {"cost": {"min": 100.0}}, "info": {},
        }

    def fetch_leverage_tiers(self, symbols):
        return {symbols[0]: [
            {"minNotional": 0, "maxNotional": 50000, "maintenanceMarginRate": 0.004,
             "maxLeverage": 125, "info": {"cum": "0"}},
        ]}

    def fetch_ohlcv(self, symbol, timeframe, since, limit):
        self.calls.append(("ohlcv", symbol, timeframe, limit))
        return [[1780000000000, 100.0, 105.0, 99.0, 104.0, 12.0]]

    def fetch_funding_rate(self, symbol):
        return {"symbol": symbol, "fundingRate": 0.0001, "fundingTimestamp": 1780041600000,
                "markPrice": 73676.1, "indexPrice": 73702.25}

    def fetch_funding_interval(self, symbol):
        return {"interval": "8h", "info": {"fundingIntervalHours": 8}}

    def fapiDataGetGlobalLongShortAccountRatio(self, params):
        self.calls.append(("lsr", params))
        return [{"symbol": params["symbol"], "longShortRatio": "1.5",
                 "longAccount": "0.6", "shortAccount": "0.4", "timestamp": "1780000000000"}]


def test_symbol_spec_wires_market_and_tiers():
    fx = FuturesExchange(FakeCcxt())
    spec = fx.symbol_spec("BTC/USDT:USDT")
    assert isinstance(spec, SymbolSpec)
    assert spec.symbol == "BTCUSDT" and spec.tick_size == 0.1


def test_ohlcv_returns_parsed_dataframe():
    fx = FuturesExchange(FakeCcxt())
    df = fx.ohlcv("BTC/USDT:USDT", "4h", 10)
    assert isinstance(df, pd.DataFrame) and df.iloc[0]["close"] == 104.0


def test_funding_returns_fundinginfo_with_interval():
    fx = FuturesExchange(FakeCcxt())
    fi = fx.funding("BTC/USDT:USDT")
    assert isinstance(fi, FundingInfo) and fi.interval_hours == 8.0


def test_long_short_ratio_uses_raw_symbol_id():
    fake = FakeCcxt()
    fx = FuturesExchange(fake)
    df = fx.long_short_ratio("BTC/USDT:USDT", "4h", 30)
    assert df.iloc[0]["long_short_ratio"] == 1.5
    # the implicit fapiData endpoint must be called with the RAW id 'BTCUSDT', not the unified symbol
    lsr_call = next(c for c in fake.calls if c[0] == "lsr")
    assert lsr_call[1]["symbol"] == "BTCUSDT"


def test_funding_interval_failure_falls_back_to_8h():
    fake = FakeCcxt()
    def boom(symbol):
        raise RuntimeError("fundingInfo unavailable")
    fake.fetch_funding_interval = boom
    fx = FuturesExchange(fake)
    assert fx.funding("BTC/USDT:USDT").interval_hours == 8.0
```

- [ ] **Step 2: Run** `uv run pytest tests/test_exchange.py -v` — expect FAIL.

- [ ] **Step 3: Implement** `futures_fund/exchange.py`:

```python
from __future__ import annotations

import pandas as pd

from futures_fund.config import Settings
from futures_fund.market_data import (
    FundingInfo,
    parse_funding,
    parse_long_short_ratio,
    parse_ohlcv,
    parse_open_interest_history,
    parse_symbol_spec,
)
from futures_fund.models import SymbolSpec


def build_ccxt(settings: Settings):
    """Construct a ccxt binanceusdm client (testnet if configured). Imported lazily so the
    test suite never needs ccxt's network stack."""
    import ccxt

    ex = ccxt.binanceusdm({
        "apiKey": settings.exchange.api_key,
        "secret": settings.exchange.api_secret,
        "enableRateLimit": True,
    })
    if settings.exchange.testnet:
        ex.set_sandbox_mode(True)
    return ex


class FuturesExchange:
    """Thin wrapper over a ccxt-like client. Inject a fake client in tests."""

    def __init__(self, client):
        self.client = client

    @classmethod
    def from_settings(cls, settings: Settings) -> "FuturesExchange":
        ex = build_ccxt(settings)
        ex.load_markets()
        return cls(ex)

    def _raw_id(self, symbol: str) -> str:
        return self.client.market(symbol)["id"]

    def symbol_spec(self, symbol: str) -> SymbolSpec:
        market = self.client.market(symbol)
        tiers = self.client.fetch_leverage_tiers([symbol])[symbol]
        return parse_symbol_spec(market, tiers)

    def ohlcv(self, symbol: str, timeframe: str = "4h", limit: int = 500) -> pd.DataFrame:
        return parse_ohlcv(self.client.fetch_ohlcv(symbol, timeframe, None, limit))

    def funding(self, symbol: str) -> FundingInfo:
        fr = self.client.fetch_funding_rate(symbol)
        try:
            interval = self.client.fetch_funding_interval(symbol)
        except Exception:
            interval = None  # symbol uses default 8h, or endpoint unavailable
        return parse_funding(fr, interval)

    def open_interest_history(self, symbol: str, period: str = "4h", limit: int = 200) -> pd.DataFrame:
        return parse_open_interest_history(
            self.client.fetch_open_interest_history(symbol, period, None, limit)
        )

    def long_short_ratio(self, symbol: str, period: str = "4h", limit: int = 200) -> pd.DataFrame:
        # implicit fapiData endpoint takes the RAW binance id, not the unified symbol
        raw = self.client.fapiDataGetGlobalLongShortAccountRatio(
            {"symbol": self._raw_id(symbol), "period": period, "limit": limit}
        )
        return parse_long_short_ratio(raw)

    def mark_price(self, symbol: str) -> float:
        return float(self.client.fetch_funding_rate(symbol)["markPrice"])
```

- [ ] **Step 4: Run** `uv run pytest tests/test_exchange.py -v` — expect PASS (5 passed).

- [ ] **Step 5: Create the manual smoke script** `scripts/smoke_testnet.py` (NOT run by pytest — `testpaths` is `tests/`):

```python
"""Manual live smoke test against the Binance USD-M testnet. Run directly:

    uv run python scripts/smoke_testnet.py

Requires BINANCE_KEY/BINANCE_SECRET (testnet) in the environment for leverage tiers;
public market data works without keys. Never imported by the test suite.
"""
from __future__ import annotations

from futures_fund.config import load_settings
from futures_fund.exchange import FuturesExchange


def main() -> None:
    fx = FuturesExchange.from_settings(load_settings())
    sym = "BTC/USDT:USDT"
    print("funding:", fx.funding(sym))
    df = fx.ohlcv(sym, "4h", 5)
    print("ohlcv tail:\n", df.tail())
    try:
        print("symbol spec:", fx.symbol_spec(sym))
    except Exception as e:  # leverage tiers need API keys
        print("symbol_spec needs API keys:", e)


if __name__ == "__main__":
    main()
```

- [ ] **Step 6: Run** `uv run ruff check futures_fund/exchange.py tests/test_exchange.py scripts/smoke_testnet.py` — fix style only.

- [ ] **Step 7: Commit**

```bash
git add futures_fund/exchange.py tests/test_exchange.py scripts/smoke_testnet.py
git commit -m "feat: FuturesExchange ccxt wrapper (testnet) + manual smoke script"
```

---

## Task 4: Data vendors (Fear&Greed, CryptoPanic, FRED) + self-archiving

**Files:** create `futures_fund/vendors.py`, `tests/test_vendors.py`.

Pure parsers tested with fixtures; fetchers tested with an injected fake HTTP client; `archive_jsonl` tested against a tmp file.

- [ ] **Step 1: Write the failing test** — `tests/test_vendors.py`:

```python
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
```

- [ ] **Step 2: Run** `uv run pytest tests/test_vendors.py -v` — expect FAIL.

- [ ] **Step 3: Implement** `futures_fund/vendors.py`:

```python
from __future__ import annotations

import json
from datetime import datetime, timezone
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
        ts=datetime.fromtimestamp(int(d["timestamp"]), tz=timezone.utc),
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


def fetch_cryptopanic(client, token: str, currencies: str = "BTC,ETH", kind: str = "news") -> list[NewsItem]:
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
            if rec.get(key) in seen:
                continue
            f.write(json.dumps(rec, default=str) + "\n")
            seen.add(rec.get(key))
            written += 1
    return written
```

- [ ] **Step 4: Run** `uv run pytest tests/test_vendors.py -v` — expect PASS (5 passed). Then `uv run ruff check futures_fund/vendors.py tests/test_vendors.py` — fix style only.

- [ ] **Step 5: Commit**

```bash
git add futures_fund/vendors.py tests/test_vendors.py
git commit -m "feat: data vendors (Fear&Greed, CryptoPanic, FRED) + JSONL self-archiver"
```

---

## Task 5: Vendor the analytical skills

**Files:** create `futures_fund/vendor/__init__.py`, `futures_fund/vendor/PROVENANCE.md`, copy 4 scripts, create `tests/test_vendor_skills.py`.

- [ ] **Step 1: Copy the scripts verbatim** (preserve their content; only the filenames change):

```bash
mkdir -p futures_fund/vendor
cp ~/.claude/skills/regime-detection/scripts/detect_regime.py        futures_fund/vendor/regime_detection.py
cp ~/.claude/skills/feature-engineering/scripts/build_features.py    futures_fund/vendor/feature_engineering.py
cp ~/.claude/skills/walk-forward-validation/scripts/walk_forward.py  futures_fund/vendor/walk_forward.py
cp ~/.claude/skills/walk-forward-validation/scripts/overfit_detector.py futures_fund/vendor/overfit_detector.py
touch futures_fund/vendor/__init__.py
```

- [ ] **Step 2: Create `futures_fund/vendor/PROVENANCE.md`:**

```markdown
# Vendored analytical scripts

Copied **verbatim** on 2026-05-29 from the user's personal Claude Code skills so the
futures-fund repo is self-contained and reproducible (spec §11 — project-only, all committed).

| File | Upstream source |
|---|---|
| `regime_detection.py` | `~/.claude/skills/regime-detection/scripts/detect_regime.py` |
| `feature_engineering.py` | `~/.claude/skills/feature-engineering/scripts/build_features.py` |
| `walk_forward.py` | `~/.claude/skills/walk-forward-validation/scripts/walk_forward.py` |
| `overfit_detector.py` | `~/.claude/skills/walk-forward-validation/scripts/overfit_detector.py` |

**Do not hand-edit** beyond import hygiene. To update, re-copy from upstream and re-run the smoke tests.
The Solana/Birdeye data-fetch helpers inside these files are unused here (A2 has its own Binance client);
we use only the pure compute functions (indicators, regime classification, features, walk-forward, DSR/PBO).
```

- [ ] **Step 3: Write the smoke test** — `tests/test_vendor_skills.py`:

```python
import importlib

import pytest


@pytest.mark.parametrize(
    "module, attr",
    [
        ("futures_fund.vendor.regime_detection", "classify_regime"),
        ("futures_fund.vendor.regime_detection", "compute_atr"),
        ("futures_fund.vendor.feature_engineering", "build_all_features"),
        ("futures_fund.vendor.walk_forward", "WalkForwardValidator"),
        ("futures_fund.vendor.overfit_detector", "deflated_sharpe_ratio"),
    ],
)
def test_vendored_module_imports_and_exposes_api(module, attr):
    m = importlib.import_module(module)
    assert hasattr(m, attr), f"{module} is missing {attr}"


def test_deflated_sharpe_ratio_runs_and_returns_probability():
    from futures_fund.vendor.overfit_detector import deflated_sharpe_ratio

    # NOTE: this function returns a DSRResult dataclass (fields include dsr_pvalue: float,
    # is_significant: bool), NOT a bare float.
    result = deflated_sharpe_ratio(observed_sr=2.0, num_trials=10, backtest_length=500)
    assert 0.0 <= result.dsr_pvalue <= 1.0  # confirms scipy imports and the computation runs
    assert isinstance(result.is_significant, bool)
```

- [ ] **Step 4: Run** `uv run pytest tests/test_vendor_skills.py -v` — expect PASS (6 passed: 5 parametrized + 1 computational). If a module fails to import due to a missing top-level dependency, STOP and report it (do not silently delete the import).

- [ ] **Step 5: Lint** `uv run ruff check futures_fund/vendor/ tests/test_vendor_skills.py`. The vendored files may trip ruff style rules (they are third-party). If so, add a per-directory ignore rather than editing vendored logic: append to `pyproject.toml`:

```toml
[tool.ruff.lint.per-file-ignores]
"futures_fund/vendor/*" = ["E", "F", "I", "UP", "B"]
```

Re-run `uv run ruff check .` — expect "All checks passed!".

- [ ] **Step 6: Run the FULL suite** `uv run pytest` — report the exact total (A1's 50 + config 4 + market_data 6 + exchange 5 + vendors 5 + vendor_skills 6 = **76**).

- [ ] **Step 7: Commit**

```bash
git add futures_fund/vendor/ tests/test_vendor_skills.py pyproject.toml
git commit -m "feat: vendor regime/feature/walk-forward/overfit analytical scripts + smoke tests"
```

---

## Self-Review (completed during planning)

**Spec coverage (§10 data + the data layer A1/B/C need):** Binance USD-M klines/funding/OI/long-short/mark + symbol spec & leverage brackets ✓ (T2/T3); testnet toggle ✓ (T3); Fear&Greed ✓, CryptoPanic ✓, FRED ✓ (T4); self-archiving of 30-day-limited endpoints ✓ (T4 `archive_jsonl`); vendored regime/feature/walk-forward/overfit ✓ (T5); typed config + env secrets ✓ (T1). Deferred (correct): wiring this data into the analyst agents and the cycle (B/A3); applying slippage from a live order book (A3 needs `fetch_order_book` → reuse A1 `slippage_cost`); predicted-vs-current funding nuance (use current rate; A3 may refine).

**Placeholder scan:** none — every step has runnable code/fixtures and exact commands.

**Type consistency:** `parse_symbol_spec` returns A1's `SymbolSpec`/`MmrBracket` (verified field names: notional_floor/cap, mmr, maint_amount, max_leverage). `FundingInfo`, `FearGreed`, `NewsItem` defined once and imported consistently. `FuturesExchange` method names match the test call sites. `load_settings`/`Settings`/`ExchangeSettings`/`DataSettings` consistent across T1.

**Known integration risks flagged for execution:** (1) ccxt is imported lazily inside `build_ccxt` so the offline suite never needs ccxt's network init — keep it lazy. (2) vendored files import `scipy` (overfit_detector) — `scipy` is added in T1; if a vendored file imports a package we didn't add (e.g. `statsmodels`), T5/Step 4 will surface it as an ImportError to report, not hide. (3) `fetch_leverage_tiers` is private (needs keys) — the smoke script handles its absence gracefully; unit tests use a fake so they never need keys.

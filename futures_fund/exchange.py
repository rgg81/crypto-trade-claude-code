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
    def from_settings(cls, settings: Settings) -> FuturesExchange:
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

    def open_interest_history(
        self, symbol: str, period: str = "4h", limit: int = 200
    ) -> pd.DataFrame:
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

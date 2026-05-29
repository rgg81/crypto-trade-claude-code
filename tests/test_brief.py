from datetime import UTC

import numpy as np
import pandas as pd

from futures_fund.brief import build_symbol_brief


class FakeExchange:
    def __init__(self, df, funding_rate=0.0001):
        self.df = df
        self.funding_rate = funding_rate

    def ohlcv(self, symbol, timeframe="4h", limit=500):
        return self.df

    def funding(self, symbol):
        from datetime import datetime

        from futures_fund.market_data import FundingInfo
        return FundingInfo(symbol=symbol, current_rate=self.funding_rate,
                           next_funding_ts=datetime(2026, 1, 1, tzinfo=UTC),
                           interval_hours=8.0, mark_price=float(self.df["close"].iloc[-1]),
                           index_price=float(self.df["close"].iloc[-1]))


def _uptrend(n=60):
    rng = np.random.default_rng(2)
    close = 100.0 + 0.7 * np.arange(n) + rng.normal(0, 0.05, n)
    return pd.DataFrame({
        "timestamp": pd.date_range("2026-01-01", periods=n, freq="4h", tz="UTC"),
        "open": close, "high": close + 0.2, "low": close - 0.2, "close": close, "volume": 1.0,
    })


def test_brief_has_expected_keys_and_types():
    b = build_symbol_brief(FakeExchange(_uptrend()), "BTC/USDT:USDT", timeframe="4h")
    assert b["symbol"] == "BTC/USDT:USDT"
    assert b["regime"] in {"low_vol_trend", "high_vol_trend", "low_vol_range",
                           "high_vol_range", "transition"}
    assert b["trend_direction"] == "up"
    assert isinstance(b["last_close"], float) and b["last_close"] > 0
    assert isinstance(b["atr"], float) and b["atr"] > 0
    assert isinstance(b["funding_rate"], float)
    assert "momentum_20" in b and isinstance(b["momentum_20"], float)


def test_brief_momentum_positive_on_uptrend():
    b = build_symbol_brief(FakeExchange(_uptrend()), "BTC/USDT:USDT")
    assert b["momentum_20"] > 0

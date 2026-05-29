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
    # the implicit fapiData endpoint must be called with the RAW id 'BTCUSDT',
    # not the unified symbol
    lsr_call = next(c for c in fake.calls if c[0] == "lsr")
    assert lsr_call[1]["symbol"] == "BTCUSDT"


def test_funding_interval_failure_falls_back_to_8h():
    fake = FakeCcxt()
    def boom(symbol):
        raise RuntimeError("fundingInfo unavailable")
    fake.fetch_funding_interval = boom
    fx = FuturesExchange(fake)
    assert fx.funding("BTC/USDT:USDT").interval_hours == 8.0

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

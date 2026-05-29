from __future__ import annotations

from futures_fund.baseline import _atr, simple_regime


def build_symbol_brief(exchange, symbol: str, timeframe: str = "4h") -> dict:
    """Compact, JSON-serializable per-symbol data bundle the orchestrator injects into the
    analyst subagents' prompts. Pure-ish: reads only from the injected exchange."""
    df = exchange.ohlcv(symbol, timeframe)
    funding = exchange.funding(symbol)
    close = df["close"]
    last = float(close.iloc[-1])
    regime = simple_regime(df)
    mom_20 = float(close.iloc[-1] / close.iloc[-21] - 1.0) if len(close) > 21 else 0.0
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "last_close": last,
        "regime": regime.quadrant,
        "trend_direction": regime.trend_direction,
        "atr": float(_atr(df)),
        "momentum_20": mom_20,
        "funding_rate": float(funding.current_rate),
        "funding_interval_hours": float(funding.interval_hours),
        "mark_price": float(funding.mark_price),
    }

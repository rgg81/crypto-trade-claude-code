from __future__ import annotations

from futures_fund.baseline import _atr, simple_regime


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
        **_derivatives(exchange, symbol, timeframe),
    }

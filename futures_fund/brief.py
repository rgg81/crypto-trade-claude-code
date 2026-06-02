from __future__ import annotations

from datetime import datetime

from futures_fund.baseline import _atr, simple_regime

_TF_SECONDS = {"15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}


def last_completed_frame(df, now: datetime | None, timeframe: str = "4h"):
    """Drop the still-FORMING last candle so 'last close', momentum, and trigger evaluation read the
    last COMPLETED bar — not a transient intra-candle print. The OHLCV feed returns the in-progress
    candle (open-ts == the current window) as the last row; if `now` falls inside that window, that
    row is dropped. An already-closed last candle (or no `now`) is left untouched, and a single-row
    frame is never emptied. ctx.prices keeps the live last row for EXITS — only completed-bar
    consumers call this."""
    if df is None or not len(df) or now is None or len(df) < 2:
        return df
    try:
        secs = _TF_SECONDS.get(timeframe, 14400)
        ts = df["timestamp"].iloc[-1]
        ts = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
        if ts.tzinfo is None:
            from datetime import UTC
            ts = ts.replace(tzinfo=UTC)
        if (now - ts).total_seconds() < secs:   # last row's window has not closed yet -> forming
            return df.iloc[:-1]
    except Exception:  # noqa: BLE001 — never break the cycle over bar housekeeping
        pass
    return df


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


def build_symbol_brief(exchange, symbol: str, timeframe: str = "4h",
                       now: datetime | None = None) -> dict:
    """Compact, JSON-serializable per-symbol data bundle the orchestrator injects into the
    analyst subagents' prompts. Pure-ish: reads only from the injected exchange. `now` (when given)
    drops the still-forming last candle so last_close/momentum/regime read the last COMPLETED bar."""
    df = last_completed_frame(exchange.ohlcv(symbol, timeframe), now, timeframe)
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

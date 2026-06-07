from __future__ import annotations

import pandas as pd

from futures_fund.models import RegimeState, TradeProposal

_EMA_SPAN = 20
_ATR_PERIOD = 14
_ATR_MULT = 2.0
_RR = 2.0
_TREND_EPS = 0.0005  # min |ema slope / price| per bar to call a trend


def _atr(df: pd.DataFrame, period: int = _ATR_PERIOD) -> float:
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([(high - low), (high - prev_close).abs(), (low - prev_close).abs()],
                   axis=1).max(axis=1)
    return float(tr.rolling(period).mean().iloc[-1])


def rsi(df: pd.DataFrame, period: int = _ATR_PERIOD) -> float:
    """Wilder's RSI on the close. 0-100; >70 overbought, <30 oversold, 50 neutral. Returns 50.0
    (neutral) on a too-short frame or any error so the brief always carries a JSON-safe number."""
    try:
        delta = df["close"].diff()
        gain = delta.clip(lower=0.0)
        loss = (-delta).clip(lower=0.0)
        avg_gain = gain.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
        avg_loss = loss.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
        rs = avg_gain / avg_loss
        val = (100.0 - 100.0 / (1.0 + rs)).iloc[-1]
        if pd.isna(val):
            return 50.0
        return float(max(0.0, min(100.0, val)))
    except Exception:  # noqa: BLE001 — an indicator must never break the brief
        return 50.0


def adx(df: pd.DataFrame, period: int = _ATR_PERIOD) -> tuple[float, float, float]:
    """Wilder's ADX (trend STRENGTH, not direction) plus +DI / -DI (direction). ADX > ~25 = a strong
    trend (do not fade); < ~20 = chop/range. +DI > -DI is up-pressure, the mirror for down. Returns
    (0.0, 0.0, 0.0) on a too-short frame or any error."""
    try:
        high, low, close = df["high"], df["low"], df["close"]
        up_move = high.diff()
        down_move = -low.diff()
        plus_dm = ((up_move > down_move) & (up_move > 0)) * up_move
        minus_dm = ((down_move > up_move) & (down_move > 0)) * down_move
        prev_close = close.shift(1)
        tr = pd.concat([(high - low), (high - prev_close).abs(), (low - prev_close).abs()],
                       axis=1).max(axis=1)
        atr = tr.ewm(alpha=1.0 / period, adjust=False).mean()
        plus_di = 100.0 * (plus_dm.ewm(alpha=1.0 / period, adjust=False).mean() / atr)
        minus_di = 100.0 * (minus_dm.ewm(alpha=1.0 / period, adjust=False).mean() / atr)
        # Replace the zero denominator with a FLOAT nan (not pd.NA): pd.NA coerces the series to
        # object dtype, and the following dx.ewm().mean() then raises TypeError (swallowed by the
        # except -> a silent (0,0,0) ADX, a real trend misread as 'no trend'). A float nan keeps
        # the series float64.
        dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0.0, float("nan"))
        adx_val = dx.ewm(alpha=1.0 / period, adjust=False).mean().iloc[-1]
        out = (adx_val, plus_di.iloc[-1], minus_di.iloc[-1])
        return tuple(0.0 if pd.isna(v) else float(v) for v in out)  # type: ignore[return-value]
    except Exception:  # noqa: BLE001
        return (0.0, 0.0, 0.0)


def ema_slope(df: pd.DataFrame, span: int = _EMA_SPAN) -> float:
    """Normalized slope of the `span`-EMA over the last 5 bars (per-bar % change). Positive = rising
    EMA. Returns 0.0 on a too-short frame (< 6 bars) or any error."""
    try:
        close = df["close"]
        if len(close) < 6:
            return 0.0
        ema = close.ewm(span=span, adjust=False).mean()
        slope = (ema.iloc[-1] - ema.iloc[-6]) / 5.0
        return float(slope / close.iloc[-1]) if close.iloc[-1] else 0.0
    except Exception:  # noqa: BLE001
        return 0.0


def swing_levels(df: pd.DataFrame, lookback: int = 20) -> tuple[float, float]:
    """Nearest structural resistance/support = the highest high / lowest low over the last
    `lookback` COMPLETED bars (a robust S/R proxy). Returns (swing_high, swing_low). Falls back to
    last close on an empty/short frame."""
    try:
        n = min(lookback, len(df))
        if n <= 0:
            last = float(df["close"].iloc[-1])
            return last, last
        return float(df["high"].tail(n).max()), float(df["low"].tail(n).min())
    except Exception:  # noqa: BLE001
        last = float(df["close"].iloc[-1])
        return last, last


def simple_regime(df: pd.DataFrame) -> RegimeState:
    close = df["close"]
    ema = close.ewm(span=_EMA_SPAN, adjust=False).mean()
    slope = (ema.iloc[-1] - ema.iloc[-6]) / 5.0
    norm_slope = slope / close.iloc[-1]
    vol = float(close.pct_change().tail(_EMA_SPAN).std())
    trending = abs(norm_slope) > _TREND_EPS
    high_vol = vol > 0.01
    direction = "up" if norm_slope > 0 else "down" if norm_slope < 0 else "neutral"
    if trending:
        quadrant = "high_vol_trend" if high_vol else "low_vol_trend"
    else:
        quadrant = "high_vol_range" if high_vol else "low_vol_range"
    return RegimeState(quadrant=quadrant, trend_direction=direction)


def propose(symbol: str, df: pd.DataFrame, funding_rate: float,
            horizon_hours: float = 4.0) -> TradeProposal | None:
    """Deterministic momentum baseline (stand-in for the Phase-B team): trade in the trend
    direction with an ATR stop and a 2R take-profit; flat when there's no trend."""
    regime = simple_regime(df)
    range_quadrants = ("low_vol_range", "high_vol_range")
    if regime.trend_direction == "neutral" or regime.quadrant in range_quadrants:
        return None
    atr = _atr(df)
    if not atr or atr <= 0:
        return None
    entry = float(df["close"].iloc[-1])
    if regime.trend_direction == "up":
        stop = entry - _ATR_MULT * atr
        tp = entry + _RR * _ATR_MULT * atr
        direction = "long"
    else:
        stop = entry + _ATR_MULT * atr
        tp = entry - _RR * _ATR_MULT * atr
        direction = "short"
    return TradeProposal(symbol=symbol, direction=direction, entry=entry, stop=stop,
                         take_profits=[tp], atr=atr, confidence=0.5,
                         horizon_hours=horizon_hours, funding_rate=funding_rate)

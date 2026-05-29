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

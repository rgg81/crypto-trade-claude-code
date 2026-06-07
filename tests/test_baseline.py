import numpy as np
import pandas as pd

from futures_fund.baseline import propose, simple_regime
from futures_fund.models import RegimeState, TradeProposal


def _trend_df(slope: float, n: int = 60, base: float = 100.0, noise: float = 0.05) -> pd.DataFrame:
    rng = np.random.default_rng(0)
    close = base + slope * np.arange(n) + rng.normal(0, noise, n)
    high = close + 0.2
    low = close - 0.2
    return pd.DataFrame({
        "timestamp": pd.date_range("2026-01-01", periods=n, freq="4h", tz="UTC"),
        "open": close, "high": high, "low": low, "close": close, "volume": 1.0,
    })


def test_simple_regime_returns_regimestate():
    r = simple_regime(_trend_df(0.5))
    assert isinstance(r, RegimeState)
    assert r.quadrant in {"low_vol_trend", "high_vol_trend", "low_vol_range",
                          "high_vol_range", "transition"}


def test_propose_long_on_clean_uptrend():
    p = propose("BTCUSDT", _trend_df(0.8), funding_rate=0.0, horizon_hours=4)
    assert isinstance(p, TradeProposal)
    assert p.direction == "long"
    assert p.stop < p.entry            # long stop below entry
    assert p.take_profits[0] > p.entry
    # reward:risk ~ 2:1 by construction
    assert (p.take_profits[0] - p.entry) / (p.entry - p.stop) >= 1.9


def test_propose_short_on_clean_downtrend():
    p = propose("BTCUSDT", _trend_df(-0.8), funding_rate=0.0, horizon_hours=4)
    assert p.direction == "short"
    assert p.stop > p.entry
    assert p.take_profits[0] < p.entry


def test_propose_flat_on_no_trend_returns_none():
    p = propose("BTCUSDT", _trend_df(0.0, noise=0.02), funding_rate=0.0, horizon_hours=4)
    assert p is None


# ---- real technical indicators (replace the LLM-invented RSI/ADX with computed values) ----

def test_rsi_uptrend_high_downtrend_low_and_bounded():
    from futures_fund.baseline import rsi
    up = rsi(_trend_df(0.8))    # steady uptrend
    down = rsi(_trend_df(-0.8))  # steady downtrend
    assert 0.0 <= up <= 100.0 and 0.0 <= down <= 100.0
    assert up > 60.0 and down < 40.0      # momentum shows in RSI
    assert up > down


def test_rsi_safe_on_short_frame():
    from futures_fund.baseline import rsi
    # fewer bars than the period must not raise; returns a neutral-ish float in [0,100]
    v = rsi(_trend_df(0.5, n=5))
    assert isinstance(v, float) and 0.0 <= v <= 100.0


def test_adx_returns_strength_and_directional_components():
    from futures_fund.baseline import adx
    a, pdi, mdi = adx(_trend_df(0.8))   # strong uptrend
    assert a >= 0.0                      # ADX is a non-negative strength measure
    assert pdi > mdi                     # uptrend -> +DI above -DI
    a2, pdi2, mdi2 = adx(_trend_df(-0.8))
    assert mdi2 > pdi2                   # downtrend -> -DI above +DI


def test_adx_handles_zero_di_sum_without_object_dtype_collapse():
    """REGRESSION: when (plus_di + minus_di) hits exactly 0.0 at any bar (leading inside/one-sided
    bars produce plus_dm==minus_dm==0), the old code did `.replace(0.0, pd.NA)`, which coerced the
    series to OBJECT dtype; the subsequent `dx.ewm().mean()` then raised TypeError, was swallowed by
    the bare except, and adx silently returned (0,0,0) — a real strong trend misread as 'no trend'
    (observed live: BTC/HYPE ADX=0 while ETH=65.6). A clearly-trending series whose early bars force
    a 0.0 DI-sum must still yield a real, positive ADX."""
    from futures_fund.baseline import adx
    n = 40
    # 5 converging INSIDE bars (force DI-sum == 0.0 early) then a clean uptrend
    highs = [120, 118, 116, 114, 112] + [112 + i for i in range(n - 5)]
    lows = [80, 82, 84, 86, 88] + [88 + i * 0.8 for i in range(n - 5)]
    close = [(h + lo) / 2 for h, lo in zip(highs, lows, strict=True)]
    df = pd.DataFrame({
        "timestamp": pd.date_range("2026-01-01", periods=n, freq="4h", tz="UTC"),
        "open": close, "high": highs, "low": lows, "close": close, "volume": 1.0,
    })
    a, pdi, mdi = adx(df)
    assert a > 0.0          # NOT the silent (0,0,0) collapse
    assert pdi > mdi        # the uptrend's +DI dominates


def test_ema_slope_sign_tracks_trend():
    from futures_fund.baseline import ema_slope
    assert ema_slope(_trend_df(0.8), 20) > 0
    assert ema_slope(_trend_df(-0.8), 20) < 0
    assert ema_slope(_trend_df(0.5, n=3), 20) == 0.0  # too-short frame -> safe 0.0


def test_swing_levels_bracket_recent_price():
    from futures_fund.baseline import swing_levels
    df = _trend_df(0.5)
    hi, lo = swing_levels(df, lookback=20)
    last = float(df["close"].iloc[-1])
    assert lo <= last <= hi          # recent swing window brackets the last close
    assert hi >= lo

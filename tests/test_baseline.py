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

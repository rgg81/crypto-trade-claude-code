from datetime import UTC, datetime

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

    def open_interest_history(self, symbol, period="4h", limit=200):
        return pd.DataFrame(
            {"timestamp": pd.date_range("2026-01-01", periods=3, freq="4h", tz="UTC"),
             "oi_amount": [100.0, 101.0, 99.0], "oi_value": [1.0e7, 1.01e7, 0.99e7]})

    def long_short_ratio(self, symbol, period="4h", limit=200):
        return pd.DataFrame(
            {"timestamp": pd.date_range("2026-01-01", periods=2, freq="4h", tz="UTC"),
             "long_short_ratio": [1.5, 1.6], "long_account": [0.6, 0.62],
             "short_account": [0.4, 0.38]})


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


def test_brief_includes_derivatives_signals():
    b = build_symbol_brief(FakeExchange(_uptrend()), "BTC/USDT:USDT")
    assert b["long_short_ratio"] == 1.6 and b["long_account"] == 0.62
    assert "oi_value" in b and b["oi_value"] > 0
    assert "oi_change" in b


def test_brief_degrades_when_derivatives_unavailable():
    class NoDeriv(FakeExchange):
        def open_interest_history(self, *a, **k):
            raise RuntimeError("unavailable")
        def long_short_ratio(self, *a, **k):
            raise RuntimeError("unavailable")
    b = build_symbol_brief(NoDeriv(_uptrend()), "BTC/USDT:USDT")
    assert b["long_short_ratio"] is None and b["oi_value"] is None


def _ohlcv(slope, n=60):
    import numpy as np
    import pandas as pd
    rng = np.random.default_rng(3)
    close = 100.0 + slope * np.arange(n) + rng.normal(0, 0.05, n)
    return pd.DataFrame({
        "timestamp": pd.date_range("2026-01-01", periods=n, freq="4h", tz="UTC"),
        "open": close, "high": close + 0.3, "low": close - 0.3, "close": close, "volume": 1.0})


def test_brief_surfaces_computed_indicators():
    ex = FakeExchange(_ohlcv(0.6))
    b = build_symbol_brief(ex, "BTC/USDT:USDT", "4h")
    for k in ("rsi", "adx", "plus_di", "minus_di", "ema20_slope", "ema50_slope",
              "swing_high", "swing_low"):
        assert k in b, f"brief missing computed indicator {k}"
    assert 0.0 <= b["rsi"] <= 100.0
    assert b["adx"] >= 0.0
    assert b["ema20_slope"] > 0           # uptrend frame -> positive slope
    assert b["swing_low"] <= b["last_close"] <= b["swing_high"]


# --- oi_change_for: REACTIVE, completed-bar-aligned OI change for the trigger OI-gate ----------
# Intentionally a shorter window than _derivatives' 48h analyst trend; drops the forming OI row so
# it matches the completed-bar frame the trigger fires on; NaN/zero-base/short/error -> None.
class _OiEx:
    """Exchange stub returning a configurable OI series (with controllable timestamps)."""
    def __init__(self, oi_values, ts_start="2026-01-01", freq="4h"):
        import pandas as pd
        n = len(oi_values)
        self._df = pd.DataFrame({
            "timestamp": pd.date_range(ts_start, periods=n, freq=freq, tz="UTC"),
            "oi_amount": [1.0] * n, "oi_value": list(oi_values)})

    def open_interest_history(self, symbol, period="4h", limit=200):
        return self._df


_FAR_NOW = datetime(2026, 6, 1, tzinfo=UTC)  # far after the 2026-01 fixtures -> nothing forming


def test_oi_change_for_positive_when_rising():
    from futures_fund.brief import oi_change_for
    ex = _OiEx([1.00e7, 1.02e7, 1.04e7, 1.06e7, 1.08e7, 1.10e7])
    v = oi_change_for(ex, "BTC/USDT:USDT", "4h", now=_FAR_NOW, lookback=4)
    assert v is not None and v > 0


def test_oi_change_for_negative_when_bleeding():
    from futures_fund.brief import oi_change_for
    ex = _OiEx([1.10e7, 1.08e7, 1.06e7, 1.04e7, 1.02e7, 1.00e7])
    v = oi_change_for(ex, "BTC/USDT:USDT", "4h", now=_FAR_NOW, lookback=4)
    assert v is not None and v < 0


def test_oi_change_for_drops_forming_row():
    # the freshest OI row is the still-FORMING window (ts within now's 4h window) and is a huge
    # spike that would flip the sign if read; aligning to completed bars must DROP it.
    from futures_fund.brief import oi_change_for
    now = datetime(2026, 6, 1, 2, tzinfo=UTC)             # inside the window opening 06-01 00:00
    ex = _OiEx([1.0e7, 1.0e7, 1.0e7, 0.9e7, 5.0e7],       # last row (06-01 00:00) is forming
               ts_start="2026-05-31 08:00")
    v = oi_change_for(ex, "BTC/USDT:USDT", "4h", now=now, lookback=4)
    assert v is not None and v <= 0   # forming 5e7 spike dropped -> completed series ends at 0.9e7


def test_oi_change_for_none_on_zero_base():
    from futures_fund.brief import oi_change_for
    ex = _OiEx([0.0, 1.0e7, 1.05e7])
    assert oi_change_for(ex, "BTC/USDT:USDT", "4h", now=_FAR_NOW) is None


def test_oi_change_for_none_on_nan():
    from futures_fund.brief import oi_change_for
    assert oi_change_for(_OiEx([float("nan"), 1.0e7, 1.05e7]), "B", "4h", now=_FAR_NOW) is None
    assert oi_change_for(_OiEx([1.0e7, 1.0e7, float("nan")]), "B", "4h", now=_FAR_NOW) is None


def test_oi_change_for_none_on_short_series():
    from futures_fund.brief import oi_change_for
    assert oi_change_for(_OiEx([1.0e7]), "B", "4h", now=_FAR_NOW) is None


def test_oi_change_for_none_on_feed_error():
    from futures_fund.brief import oi_change_for
    class _Boom:
        def open_interest_history(self, *a, **k):
            raise RuntimeError("feed down")
    assert oi_change_for(_Boom(), "B", "4h", now=_FAR_NOW) is None


def test_flag_duplicate_positioning_nulls_aliased_feed():
    """DATA-INTEGRITY (cy50): the globalLongShortAccountRatio feed aliased DOGE onto ETH (identical
    long_short_ratio 2.3456 AND long_account 0.7011). Distinct symbols sharing the SAME non-null
    (L/S, long_account) pair = a feed-alias bug; null positioning for EVERY member + flag, since we
    cannot tell which is correct. Distinct values and None positioning are left untouched."""
    from futures_fund.brief import flag_duplicate_positioning
    briefs = [
        {"exchange_id": "ETHUSDT", "long_short_ratio": 2.3456, "long_account": 0.7011},
        {"exchange_id": "DOGEUSDT", "long_short_ratio": 2.3456, "long_account": 0.7011},  # aliased
        {"exchange_id": "BTCUSDT", "long_short_ratio": 2.0184, "long_account": 0.6687},  # distinct
        {"exchange_id": "XRPUSDT", "long_short_ratio": None, "long_account": None},  # no feed
    ]
    out = flag_duplicate_positioning(briefs)
    by = {b["exchange_id"]: b for b in out}
    # the colliding pair: BOTH nulled + flagged (cannot tell which symbol owns 2.3456)
    for sym in ("ETHUSDT", "DOGEUSDT"):
        assert by[sym]["long_short_ratio"] is None
        assert by[sym]["long_account"] is None
        assert by[sym]["positioning_anomaly"] == "duplicate_ls_feed"
    # distinct symbol untouched, no anomaly flag
    assert by["BTCUSDT"]["long_short_ratio"] == 2.0184
    assert "positioning_anomaly" not in by["BTCUSDT"]
    # None-positioning symbol is ignored (not grouped, not flagged)
    assert "positioning_anomaly" not in by["XRPUSDT"]


def test_flag_duplicate_positioning_ignores_same_symbol_repeat():
    """A symbol appearing twice with identical positioning (e.g. a regime-panel duplicate) is NOT
    an alias — only DISTINCT symbols colliding indicates the feed bug. Leave it untouched."""
    from futures_fund.brief import flag_duplicate_positioning
    briefs = [
        {"exchange_id": "ETHUSDT", "long_short_ratio": 2.3456, "long_account": 0.7011},
        {"exchange_id": "ETHUSDT", "long_short_ratio": 2.3456, "long_account": 0.7011},
    ]
    out = flag_duplicate_positioning(briefs)
    for b in out:
        assert b["long_short_ratio"] == 2.3456
        assert "positioning_anomaly" not in b

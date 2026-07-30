from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

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


def test_funding_direction_helper_is_unambiguous():
    # cy78 fix: two agents inverted the funding sign. The brief must carry an explicit PAYER so no
    # agent has to interpret the raw sign. Convention: rate>0 -> longs pay shorts; rate<0 -> shorts
    # pay longs; rate==0 -> none. annualized% = rate * (24/interval) * 365 * 100, signed.
    from futures_fund.brief import funding_direction
    assert funding_direction(0.0001, 8.0) == ("longs", pytest.approx(10.95, abs=0.1))
    assert funding_direction(-0.000968, 8.0)[0] == "shorts"
    assert funding_direction(-0.000968, 8.0)[1] == pytest.approx(-105.99, abs=0.2)
    assert funding_direction(0.0, 8.0) == ("none", 0.0)
    # the TRX case that bit us: NEGATIVE rate -> SHORTS pay -> a short position has a carry DRAG
    payer, _ = funding_direction(-0.000968, 8.0)
    assert payer == "shorts"          # a SHORT pays; a LONG receives — never the inverse


def test_funding_premium_classifies_relative_to_the_zero_premium_baseline():
    """cy309: `funding_payer == 'longs'` is true for ANY rate > 0, so the desk read the Binance
    baseline itself as 'longs bleeding carry'. Funding = premium + clamp(interest - premium, ±5bp),
    so a perp whose basis sits inside the clamp prints EXACTLY the interest rate (0.01%/8h =
    10.95%/yr) with payer='longs'. At par is therefore INDETERMINATE, not zero-premium; only
    above/below baseline carry information."""
    from futures_fund.brief import (
        FUNDING_BASELINE_ANNUAL_PCT,
        funding_premium,
    )
    assert FUNDING_BASELINE_ANNUAL_PCT == pytest.approx(10.95, abs=0.01)
    # exactly at baseline -> at_par (INDETERMINATE), NOT a carry edge
    state, ratio = funding_premium(10.95)
    assert state == "at_par" and ratio == pytest.approx(1.0, abs=0.01)
    # below baseline but still payer='longs' -> a genuine perp DISCOUNT, not longs paying up
    state, ratio = funding_premium(7.06)          # cy309 BNB
    assert state == "discount" and ratio == pytest.approx(0.645, abs=0.005)
    state, _ = funding_premium(1.07)              # cy309 SUI
    assert state == "discount"
    # genuinely elevated -> a real premium (the only case that is flush-short carry fuel)
    state, ratio = funding_premium(46.1)          # the LAB case: ~4.2x baseline
    assert state == "premium" and ratio == pytest.approx(4.21, abs=0.02)
    # negative funding is a discount too (shorts pay)
    assert funding_premium(-11.12)[0] == "discount"
    # degrade safely
    assert funding_premium(None) == ("unknown", None)


def test_brief_carries_funding_premium_fields():
    # the baseline-relative read must reach the agents, not just the raw sign
    b = build_symbol_brief(FakeExchange(_uptrend(), funding_rate=0.0001), "BTC/USDT:USDT")
    assert b["funding_baseline_pct"] == pytest.approx(10.95, abs=0.01)
    assert b["funding_premium"] == "at_par"        # the default rate IS the baseline
    assert b["funding_vs_baseline"] == pytest.approx(1.0, abs=0.01)


def test_brief_carries_explicit_funding_payer_and_annualized():
    # positive funding (FakeExchange default +0.0001) => longs pay shorts
    b = build_symbol_brief(FakeExchange(_uptrend(), funding_rate=0.0001), "BTC/USDT:USDT")
    assert b["funding_payer"] == "longs"
    assert b["funding_annualized_pct"] == pytest.approx(10.95, abs=0.1)
    # negative funding => shorts pay longs (the TRX trap)
    bn = build_symbol_brief(FakeExchange(_uptrend(), funding_rate=-0.000968), "BTC/USDT:USDT")
    assert bn["funding_payer"] == "shorts"
    assert bn["funding_annualized_pct"] < 0


def test_brief_includes_derivatives_signals():
    b = build_symbol_brief(FakeExchange(_uptrend()), "BTC/USDT:USDT")
    assert b["long_short_ratio"] == 1.6 and b["long_account"] == 0.62
    assert "oi_value" in b and b["oi_value"] > 0
    assert "oi_change" in b


def test_brief_carries_coin_count_oi_alongside_usd_notional():
    """cy311: `oi_change` is USD NOTIONAL = contracts x price, so its % change conflates positioning
    with price — and the bias is DIRECTIONAL: in a falling market USD-OI drops even while contracts
    build, which is exactly what manufactured the desk's 13-cycle 'no with-regime short has rising
    OI' reading. HYPE printed -2.96% USD but +1.08% in contracts. Surface both."""
    class Diverging(FakeExchange):
        def open_interest_history(self, symbol, period="4h", limit=200):
            # contracts BUILD +10% while price halves -> USD notional FALLS 45%
            return pd.DataFrame(
                {"timestamp": pd.date_range("2026-01-01", periods=3, freq="4h", tz="UTC"),
                 "oi_amount": [100.0, 105.0, 110.0], "oi_value": [1.0e7, 0.75e7, 0.55e7]})
    b = build_symbol_brief(Diverging(_uptrend()), "BTC/USDT:USDT")
    assert b["oi_change"] < 0                       # USD notional says "exhausting"
    assert b["oi_change_coin"] == pytest.approx(0.10, abs=1e-9)   # contracts say BUILDING
    assert b["oi_amount"] == pytest.approx(110.0)
    # and the two must be reported independently, never collapsed into one number
    assert b["oi_change"] != b["oi_change_coin"]


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


def test_derivatives_drops_forming_ls_and_oi_bar():
    """_derivatives must read the LAST COMPLETED bar for the OI-trend AND long/short positioning —
    the same forming-candle discipline OHLCV and the OI trigger-gate already apply — so the brief's
    positioning matches its completed-bar price. This ALSO sidesteps the simulated
    globalLongShortAccountRatio feed-alias, which is byte-identical only on the FORMING bar (cy50:
    DOGE==ETH on the in-progress candle) while the CLOSED bar is clean/distinct per symbol."""
    from futures_fund.brief import _derivatives
    t_prev = datetime(2026, 6, 7, 4, 0, tzinfo=UTC)
    t_closed = datetime(2026, 6, 7, 8, 0, tzinfo=UTC)
    t_forming = datetime(2026, 6, 7, 12, 0, tzinfo=UTC)
    now = t_forming + timedelta(minutes=36)  # inside the 12:00-16:00 window -> 12:00 is FORMING
    ts = [t_prev, t_closed, t_forming]

    class _Ex:
        def open_interest_history(self, symbol, period="4h", limit=12):
            # 999 = forming, must be dropped
            return pd.DataFrame({"timestamp": ts, "oi_value": [100.0, 110.0, 999.0]})

        def long_short_ratio(self, symbol, period="4h", limit=6):
            # 2.3456 / 0.7011 = the aliased FORMING bar, must be dropped
            return pd.DataFrame({"timestamp": ts,
                                 "long_short_ratio": [2.30, 2.3156, 2.3456],
                                 "long_account": [0.69, 0.6984, 0.7011],
                                 "short_account": [0.31, 0.3016, 0.2989]})

    out = _derivatives(_Ex(), "DOGEUSDT", "4h", now=now)
    # reads the CLOSED 08:00 values, NOT the forming 12:00 aliased ones
    assert out["long_short_ratio"] == 2.3156
    assert out["long_account"] == 0.6984
    assert out["oi_value"] == 110.0  # forming 999 dropped
    assert abs(out["oi_change"] - (110.0 / 100.0 - 1.0)) < 1e-9  # 0.10 over completed bars


def test_derivatives_keeps_last_bar_when_closed_or_no_now():
    """Backward-compat: with now=None (or an already-closed last bar) no row is dropped."""
    from futures_fund.brief import _derivatives
    ts = pd.date_range("2026-06-01", periods=3, freq="4h", tz="UTC")

    class _Ex:
        def open_interest_history(self, symbol, period="4h", limit=12):
            return pd.DataFrame({"timestamp": ts, "oi_value": [100.0, 110.0, 120.0]})

        def long_short_ratio(self, symbol, period="4h", limit=6):
            return pd.DataFrame({"timestamp": ts, "long_short_ratio": [2.0, 2.1, 2.2],
                                 "long_account": [0.66, 0.67, 0.6875],
                                 "short_account": [0.34, 0.33, 0.3125]})

    out = _derivatives(_Ex(), "X", "4h", now=None)  # no now -> keep the last row
    assert out["long_short_ratio"] == 2.2
    assert out["oi_value"] == 120.0


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

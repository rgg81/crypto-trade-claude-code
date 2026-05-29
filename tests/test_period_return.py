from datetime import UTC, datetime

import pytest

from futures_fund.equity_log import period_return, record_equity


def test_period_return_uses_baseline_before_cutoff(tmp_path):
    record_equity(tmp_path, datetime(2026, 5, 1, tzinfo=UTC), 10_000.0, cycle=1)
    record_equity(tmp_path, datetime(2026, 5, 2, tzinfo=UTC), 10_300.0, cycle=2)  # +3% over 1 day
    now = datetime(2026, 5, 2, tzinfo=UTC)
    assert period_return(tmp_path, now, days=1) == pytest.approx(0.03)


def test_period_return_negative_drawdown(tmp_path):
    record_equity(tmp_path, datetime(2026, 5, 1, tzinfo=UTC), 10_000.0, cycle=1)
    record_equity(tmp_path, datetime(2026, 5, 1, 12, tzinfo=UTC), 9_600.0, cycle=2)  # -4%
    now = datetime(2026, 5, 1, 12, tzinfo=UTC)
    assert period_return(tmp_path, now, days=1) == pytest.approx(-0.04)


def test_period_return_too_little_history_is_zero(tmp_path):
    record_equity(tmp_path, datetime(2026, 5, 1, tzinfo=UTC), 10_000.0, cycle=1)
    assert period_return(tmp_path, datetime(2026, 5, 1, tzinfo=UTC), days=1) == 0.0


def test_daily_breaker_blocks_new_entry_in_the_cycle(tmp_path):
    import numpy as np
    import pandas as pd

    from futures_fund.config import Settings
    from futures_fund.cycle import execute_proposals, fetch_context
    from futures_fund.models import MmrBracket, SymbolSpec, TradeProposal
    from futures_fund.state import AccountState

    class FakeExchange:
        def __init__(self, df):
            self.df = df

        def symbol_spec(self, s):
            return SymbolSpec(symbol="BTCUSDT", tick_size=0.01, step_size=0.001, min_notional=5.0,
                              mmr_brackets=[MmrBracket(notional_floor=0, notional_cap=1_000_000,
                                                       mmr=0.004, maint_amount=0.0,
                                                       max_leverage=125)])

        def ohlcv(self, s, tf="4h", limit=500):
            return self.df

        def funding(self, s):
            from futures_fund.market_data import FundingInfo
            return FundingInfo(symbol=s, current_rate=0.0,
                               next_funding_ts=datetime(2026, 1, 1, tzinfo=UTC), interval_hours=8.0,
                               mark_price=float(self.df["close"].iloc[-1]),
                               index_price=float(self.df["close"].iloc[-1]))

    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    from futures_fund.memory_layout import ensure_memory_layout
    ensure_memory_layout(memory_dir)
    # pre-seed an equity history showing a -5% day -> daily breaker should halt new entries
    record_equity(state_dir, datetime(2026, 3, 1, tzinfo=UTC), 10_000.0, cycle=1)
    record_equity(state_dir, datetime(2026, 3, 1, 12, tzinfo=UTC), 9_500.0, cycle=2)
    close = 100.0 + 0.8 * np.arange(60) + np.random.default_rng(5).normal(0, 0.05, 60)
    df = pd.DataFrame({"timestamp": pd.date_range("2026-01-01", periods=60, freq="4h", tz="UTC"),
                       "open": close, "high": close + 0.2, "low": close - 0.2,
                       "close": close, "volume": 1.0})
    ex = FakeExchange(df)
    ctx = fetch_context(ex, Settings(symbols=["BTC/USDT:USDT"]))
    last = float(df["close"].iloc[-1])
    prop = TradeProposal(symbol="BTCUSDT", direction="long", entry=last, stop=last - 4.0,
                         take_profits=[last + 8.0], atr=2.0, confidence=0.7, horizon_hours=4,
                         funding_rate=0.0)
    report = execute_proposals(ctx, [prop], ["team"], [],
                               AccountState(balance=9_500.0, peak_equity=10_000.0),
                               state_dir, memory_dir,
                               now=datetime(2026, 3, 1, 12, tzinfo=UTC), cycle_no=3)
    assert report["opened"] == 0  # daily breaker (-5%) vetoes the entry

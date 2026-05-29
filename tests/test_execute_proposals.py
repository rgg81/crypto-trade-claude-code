from datetime import UTC, datetime

import numpy as np
import pandas as pd

from futures_fund.config import Settings
from futures_fund.cycle import execute_proposals, fetch_context
from futures_fund.models import TradeProposal
from futures_fund.state import AccountState, load_positions


class FakeExchange:
    def __init__(self, frames):
        self.frames = frames

    def symbol_spec(self, symbol):
        from futures_fund.models import MmrBracket, SymbolSpec
        return SymbolSpec(symbol="BTCUSDT", tick_size=0.01, step_size=0.001, min_notional=5.0,
                          mmr_brackets=[MmrBracket(notional_floor=0, notional_cap=1_000_000,
                                                   mmr=0.004, maint_amount=0.0, max_leverage=125)])

    def ohlcv(self, symbol, timeframe="4h", limit=500):
        return self.frames[symbol]

    def funding(self, symbol):
        from futures_fund.market_data import FundingInfo
        return FundingInfo(symbol=symbol, current_rate=0.0,
                           next_funding_ts=datetime(2026, 1, 1, tzinfo=UTC), interval_hours=8.0,
                           mark_price=float(self.frames[symbol]["close"].iloc[-1]),
                           index_price=float(self.frames[symbol]["close"].iloc[-1]))


def _uptrend(n=60):
    rng = np.random.default_rng(3)
    close = 100.0 + 0.8 * np.arange(n) + rng.normal(0, 0.05, n)
    return pd.DataFrame({
        "timestamp": pd.date_range("2026-01-01", periods=n, freq="4h", tz="UTC"),
        "open": close, "high": close + 0.2, "low": close - 0.2, "close": close, "volume": 1.0,
    })


def _settings():
    return Settings(account_size_usdt=10_000.0, symbols=["BTC/USDT:USDT"], timeframe="4h")


def test_execute_proposals_opens_and_journals(tmp_path):
    state_dir, memory_dir = tmp_path / "state", tmp_path / "memory"
    from futures_fund.memory_layout import ensure_memory_layout
    ensure_memory_layout(memory_dir)
    ex = FakeExchange({"BTC/USDT:USDT": _uptrend()})
    ctx = fetch_context(ex, _settings())
    last = float(ctx.frames["BTC/USDT:USDT"]["close"].iloc[-1])
    prop = TradeProposal(symbol="BTCUSDT", direction="long", entry=last, stop=last - 4.0,
                         take_profits=[last + 8.0], atr=2.0, confidence=0.7,
                         horizon_hours=4, funding_rate=0.0)
    account = AccountState(balance=10_000.0, peak_equity=10_000.0)
    report = execute_proposals(ctx, [prop], contributing_agents=["research_manager", "trader"],
                               positions=[], account=account, state_dir=state_dir,
                               memory_dir=memory_dir, now=datetime(2026, 3, 1, tzinfo=UTC),
                               cycle_no=1)
    assert report["opened"] == 1
    pos = load_positions(state_dir)
    assert len(pos) == 1 and pos[0].decision_id is not None


def test_execute_proposals_empty_book_opens_nothing(tmp_path):
    state_dir, memory_dir = tmp_path / "state", tmp_path / "memory"
    from futures_fund.memory_layout import ensure_memory_layout
    ensure_memory_layout(memory_dir)
    ex = FakeExchange({"BTC/USDT:USDT": _uptrend()})
    ctx = fetch_context(ex, _settings())
    account = AccountState(balance=10_000.0, peak_equity=10_000.0)
    report = execute_proposals(ctx, [], contributing_agents=["trader"], positions=[],
                               account=account, state_dir=state_dir, memory_dir=memory_dir,
                               now=datetime(2026, 3, 1, tzinfo=UTC), cycle_no=1)
    assert report["opened"] == 0
    assert load_positions(state_dir) == []


def test_baseline_run_cycle_still_works(tmp_path):
    # the refactor must not break the baseline path
    from futures_fund.cycle import run_cycle
    ex = FakeExchange({"BTC/USDT:USDT": _uptrend()})
    report = run_cycle(ex, _settings(), tmp_path / "s", tmp_path / "m",
                       now=datetime(2026, 3, 1, tzinfo=UTC), cycle_no=1)
    assert report["opened"] == 1

from datetime import UTC, datetime

import numpy as np
import pandas as pd

from futures_fund.config import Settings
from futures_fund.cycle import run_cycle
from futures_fund.journal import read_all_decisions, read_open_decisions
from futures_fund.models import MmrBracket, SymbolSpec
from futures_fund.state import load_account, load_positions


class FakeExchange:
    """Injected stand-in for FuturesExchange returning scripted data per symbol."""

    def __init__(self, frames: dict[str, pd.DataFrame], funding_rate: float = 0.0):
        self.frames = frames
        self.funding_rate = funding_rate

    def symbol_spec(self, symbol):
        return SymbolSpec(symbol=symbol.split("/")[0] + "USDT" if "/" in symbol else symbol,
                          tick_size=0.01, step_size=0.001, min_notional=5.0,
                          mmr_brackets=[MmrBracket(notional_floor=0, notional_cap=1_000_000,
                                                   mmr=0.004, maint_amount=0.0, max_leverage=125)])

    def ohlcv(self, symbol, timeframe="4h", limit=500):
        return self.frames[symbol]

    def funding(self, symbol):
        from futures_fund.market_data import FundingInfo
        return FundingInfo(
            symbol=symbol, current_rate=self.funding_rate,
            next_funding_ts=datetime(2026, 1, 1, tzinfo=UTC),
            interval_hours=8.0,
            mark_price=float(self.frames[symbol]["close"].iloc[-1]),
            index_price=float(self.frames[symbol]["close"].iloc[-1]),
        )

    def mark_price(self, symbol):
        return float(self.frames[symbol]["close"].iloc[-1])


def _frame(closes, highs=None, lows=None):
    n = len(closes)
    closes = np.asarray(closes, dtype=float)
    high = np.asarray(highs, dtype=float) if highs is not None else closes + 0.2
    low = np.asarray(lows, dtype=float) if lows is not None else closes - 0.2
    return pd.DataFrame({
        "timestamp": pd.date_range("2026-01-01", periods=n, freq="4h", tz="UTC"),
        "open": closes, "high": high, "low": low, "close": closes, "volume": 1.0,
    })


def _uptrend(n=60, base=100.0, slope=0.8):
    rng = np.random.default_rng(1)
    return _frame(base + slope * np.arange(n) + rng.normal(0, 0.05, n))


def _settings():
    return Settings(account_size_usdt=10_000.0, symbols=["BTC/USDT:USDT"], timeframe="4h")


def test_cycle_runs_end_to_end_and_opens_a_position(tmp_path):
    state_dir, memory_dir = tmp_path / "state", tmp_path / "memory"
    ex = FakeExchange({"BTC/USDT:USDT": _uptrend()})
    report = run_cycle(ex, _settings(), state_dir, memory_dir,
                       now=datetime(2026, 3, 1, tzinfo=UTC), cycle_no=1)
    # the clean uptrend should produce one approved long
    assert report["opened"] == 1
    positions = load_positions(state_dir)
    assert len(positions) == 1 and positions[0].direction == "long"
    # a Phase-1 decision was journaled and is still open (no outcome yet)
    assert len(read_open_decisions(memory_dir)) == 1
    # account + memory artifacts persisted
    assert (state_dir / "account.json").exists()
    assert (memory_dir / "semantic" / "beliefs.md").exists()


def test_second_cycle_closes_position_on_crash(tmp_path):
    state_dir, memory_dir = tmp_path / "state", tmp_path / "memory"
    ex1 = FakeExchange({"BTC/USDT:USDT": _uptrend()})
    run_cycle(ex1, _settings(), state_dir, memory_dir,
              now=datetime(2026, 3, 1, tzinfo=UTC), cycle_no=1)
    pos = load_positions(state_dir)[0]
    # build a frame whose final bar crashes below the open position's stop
    crash = _uptrend()
    crash.loc[crash.index[-1], ["low", "close"]] = pos.stop - 5.0
    ex2 = FakeExchange({"BTC/USDT:USDT": crash})
    report = run_cycle(ex2, _settings(), state_dir, memory_dir,
                       now=datetime(2026, 3, 1, 4, tzinfo=UTC), cycle_no=2)
    assert report["closed"] >= 1
    # the decision now has a realized outcome (Phase-2 patched)
    closed = [d for d in read_all_decisions(memory_dir) if d.get("realized_pnl") is not None]
    assert len(closed) >= 1


def test_halt_flag_skips_trading(tmp_path):
    from futures_fund.state import AccountState, save_account, set_halt
    state_dir, memory_dir = tmp_path / "state", tmp_path / "memory"
    load_account(state_dir, 10_000.0)  # create account file
    save_account(state_dir, AccountState(balance=10_000.0, peak_equity=10_000.0))
    set_halt(state_dir, True, reason="test")
    ex = FakeExchange({"BTC/USDT:USDT": _uptrend()})
    report = run_cycle(ex, _settings(), state_dir, memory_dir,
                       now=datetime(2026, 3, 1, tzinfo=UTC), cycle_no=1)
    assert report["halted"] is True
    assert report["opened"] == 0
    assert load_positions(state_dir) == []

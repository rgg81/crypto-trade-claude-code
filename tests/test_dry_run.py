from datetime import UTC, datetime

import numpy as np
import pandas as pd

from futures_fund.config import Settings
from futures_fund.cycle_io import load_output, save_output
from futures_fund.orchestration import (
    gate_execute_step,
    preflight_step,
    reclassify_step,
    screen_step,
)
from futures_fund.state import load_positions


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
        return FundingInfo(symbol=symbol, current_rate=0.0001,
                           next_funding_ts=datetime(2026, 1, 1, tzinfo=UTC), interval_hours=8.0,
                           mark_price=float(self.frames[symbol]["close"].iloc[-1]),
                           index_price=float(self.frames[symbol]["close"].iloc[-1]))


def _uptrend(n=60):
    rng = np.random.default_rng(11)
    close = 100.0 + 0.8 * np.arange(n) + rng.normal(0, 0.05, n)
    return pd.DataFrame({
        "timestamp": pd.date_range("2026-01-01", periods=n, freq="4h", tz="UTC"),
        "open": close, "high": close + 0.2, "low": close - 0.2, "close": close, "volume": 1.0,
    })


def test_full_orchestration_dry_run_with_fixture_agents(tmp_path):
    """Simulate the orchestrator: preflight -> (fixture analyst reports) -> screen ->
    (fixture trader proposal) -> gate/execute. Proves the plumbing with no live LLM."""
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    ex = FakeExchange({"BTC/USDT:USDT": _uptrend()})
    settings = Settings(account_size_usdt=10_000.0, symbols=["BTC/USDT:USDT"], timeframe="4h")
    now = datetime(2026, 3, 1, tzinfo=UTC)

    # Phase 0-2: preflight produces briefs/context
    pf = preflight_step(ex, settings, state_dir, memory_dir, now=now, cycle_no=1)
    save_output(state_dir, 1, "context", pf)
    last = pf["briefs"][0]["last_close"]

    # Phase 4: orchestrator would dispatch analyst subagents; here we stand in fixture reports.
    # The news report carries a risk_off_flag so Phase 4.6 has a real signal to fold.
    reports = [{"agent": a, "symbol": "BTCUSDT", "stance": "bullish", "confidence": 0.8,
                "signals": {"risk_off_flag": 1} if a == "news" else {}}
               for a in ("technical", "derivatives", "news", "sentiment")]
    save_output(state_dir, 1, "analyst_reports", reports)

    # Phase 4.5: screen
    screened = screen_step(reports, top_n=5)
    save_output(state_dir, 1, "screened", {"symbols": screened})
    assert screened == ["BTCUSDT"]

    # Phase 4.6: re-classify the regime with the News analyst's risk_off_flag, overwrite context.
    # This MUST run between screen and the gate or the news fold is silently skipped (review FIX 5).
    pre_news = load_output(state_dir, 1, "context")["regime_state"]["drivers"]["news_risk_off"]
    assert pre_news is None  # preflight had no news judgment yet (degraded)
    regime_state = reclassify_step(state_dir, load_output(state_dir, 1, "context"), reports)
    ctx = load_output(state_dir, 1, "context")
    ctx["regime_state"] = regime_state
    save_output(state_dir, 1, "context", ctx)
    assert regime_state["drivers"]["news_risk_off"] is True  # the fold engaged

    # Phases 5-6: orchestrator dispatches Bull/Bear/RM/Trader; fixture trader proposal
    proposal = {"symbol": "BTCUSDT", "direction": "long", "entry": last, "stop": last - 4.0,
                "take_profits": [last + 8.0], "atr": 2.0, "confidence": 0.7,
                "rationale": "uptrend + funding tailwind; bear's mean-reversion case rejected"}
    save_output(state_dir, 1, "proposals", {"proposals": [proposal]})

    # Phases 7-10: gate + execute, reading the news-informed regime_state from context
    proposals = load_output(state_dir, 1, "proposals")["proposals"]
    regime_state = load_output(state_dir, 1, "context")["regime_state"]
    report = gate_execute_step(ex, settings, state_dir, memory_dir, now=now, cycle_no=1,
                               proposals=proposals, regime_state=regime_state)
    # The 1-symbol harness can't reach regime quorum (needs >=3 majors), so the read is
    # untrustworthy -> fail-closed SYMMETRIC: the fresh long is converted to a confirmation
    # stop_entry trigger, not opened at market. (A real multi-major cycle with quorum opens a
    # with-regime entry at market -- see test_gate_wiring.) News folding blocked nothing.
    assert report["opened"] == 0 and report["counter_regime_triggered"] == 1
    assert report["triggers_armed"] == 1
    assert report["news_folded"] is True  # the report proves Phase 4.6 engaged
    assert len(load_positions(state_dir)) == 0  # converted to a trigger, not a position
    # the per-cycle workspace persisted each stage (no silent runs)
    assert load_output(state_dir, 1, "context")["cycle"] == 1
    assert load_output(state_dir, 1, "screened")["symbols"] == ["BTCUSDT"]

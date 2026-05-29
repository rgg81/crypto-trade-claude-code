import datetime as dt
from datetime import datetime

import numpy as np
import pandas as pd

from futures_fund.config import Settings
from futures_fund.contracts import AgentProposal
from futures_fund.orchestration import gate_execute_step, preflight_step, reflect_step, screen_step
from futures_fund.state import load_positions

UTC = dt.UTC

_RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel><item>
<title>BTC chops sideways</title><link>http://x/1</link>
<pubDate>Fri, 29 May 2026 14:20:32 +0000</pubDate></item></channel></rss>"""


class _Resp:
    def __init__(self, *, content=b"", payload=None, status=200):
        self.content = content
        self._p = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("http")

    def json(self):
        return self._p


class _HttpClient:
    def get(self, url, params=None, **kw):
        if "alternative.me" in url:
            return _Resp(payload={"data": [{"value": "30",
                                            "value_classification": "Fear",
                                            "timestamp": "1780012800"}]})
        return _Resp(content=_RSS)


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
                           next_funding_ts=dt.datetime(2026, 1, 1, tzinfo=UTC), interval_hours=8.0,
                           mark_price=float(self.frames[symbol]["close"].iloc[-1]),
                           index_price=float(self.frames[symbol]["close"].iloc[-1]))

    def open_interest_history(self, symbol, period="4h", limit=200):
        import pandas as pd
        return pd.DataFrame({"timestamp": pd.date_range("2026-01-01", periods=3, freq="4h",
                                                        tz="UTC"),
                             "oi_amount": [1., 1., 1.], "oi_value": [1e7, 1e7, 1e7]})

    def long_short_ratio(self, symbol, period="4h", limit=200):
        import pandas as pd
        return pd.DataFrame({"timestamp": pd.date_range("2026-01-01", periods=2, freq="4h",
                                                        tz="UTC"),
                             "long_short_ratio": [1.5, 1.6], "long_account": [0.6, 0.62],
                             "short_account": [0.4, 0.38]})


def _uptrend(n=60):
    rng = np.random.default_rng(7)
    close = 100.0 + 0.8 * np.arange(n) + rng.normal(0, 0.05, n)
    return pd.DataFrame({
        "timestamp": pd.date_range("2026-01-01", periods=n, freq="4h", tz="UTC"),
        "open": close, "high": close + 0.2, "low": close - 0.2, "close": close, "volume": 1.0,
    })


def _settings():
    return Settings(account_size_usdt=10_000.0, symbols=["BTC/USDT:USDT"], timeframe="4h")


def test_preflight_emits_context_with_briefs(tmp_path):
    ex = FakeExchange({"BTC/USDT:USDT": _uptrend()})
    ctx = preflight_step(ex, _settings(), tmp_path / "s", tmp_path / "m",
                         now=dt.datetime(2026, 3, 1, tzinfo=UTC), cycle_no=1,
                         http_client=_HttpClient())
    assert ctx["cycle"] == 1
    assert ctx["halted"] is False
    assert "BTC/USDT:USDT" in {b["symbol"] for b in ctx["briefs"]}
    assert ctx["briefs"][0]["regime"]  # brief carries the regime
    assert "equity" in ctx and ctx["equity"] > 0


def test_screen_step_returns_top_symbols(tmp_path):
    reports = [
        {"agent": "technical", "symbol": "BTCUSDT", "stance": "bullish", "confidence": 0.9},
        {"agent": "derivatives", "symbol": "BTCUSDT", "stance": "bullish", "confidence": 0.8},
        {"agent": "technical", "symbol": "ETHUSDT", "stance": "neutral", "confidence": 0.5},
    ]
    top = screen_step(reports, top_n=5)
    assert top == ["BTCUSDT"]


def test_gate_execute_step_opens_from_agent_proposals(tmp_path):
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    ex = FakeExchange({"BTC/USDT:USDT": _uptrend()})
    pf = preflight_step(ex, _settings(), state_dir, memory_dir,
                        now=dt.datetime(2026, 3, 1, tzinfo=UTC), cycle_no=1,
                        http_client=_HttpClient())
    last = pf["briefs"][0]["last_close"]
    proposals = [AgentProposal(symbol="BTCUSDT", direction="long", entry=last,
                               stop=last - 4.0, take_profits=[last + 8.0], atr=2.0,
                               confidence=0.7, rationale="bull thesis won the debate").model_dump()]
    report = gate_execute_step(ex, _settings(), state_dir, memory_dir,
                               now=dt.datetime(2026, 3, 1, tzinfo=UTC), cycle_no=1,
                               proposals=proposals)
    assert report["opened"] == 1
    pos = load_positions(state_dir)
    assert len(pos) == 1 and pos[0].decision_id is not None


def test_reflect_step_splits_winners_losers(tmp_path):
    from futures_fund.journal import append_decision, patch_outcome
    from futures_fund.memory_layout import ensure_memory_layout
    memory_dir = tmp_path / "m"
    ensure_memory_layout(memory_dir)
    did = append_decision(memory_dir, {"ts": dt.datetime(2026, 5, 1, tzinfo=UTC), "cycle": 1,
                                       "symbol": "BTCUSDT", "direction": "long",
                                       "entry": 100.0, "stop": 95.0})
    patch_outcome(memory_dir, did, {"realized_pnl": 42.0, "prediction_correct": True})
    payload = reflect_step(memory_dir)
    assert payload["n_closed"] == 1 and len(payload["winners"]) == 1


def test_preflight_brief_includes_exchange_id(tmp_path):
    ex = FakeExchange({"BTC/USDT:USDT": _uptrend()})
    ctx = preflight_step(ex, _settings(), tmp_path / "s", tmp_path / "m",
                         now=dt.datetime(2026, 3, 1, tzinfo=UTC), cycle_no=1,
                         http_client=_HttpClient())
    assert ctx["briefs"][0]["exchange_id"] == "BTCUSDT"


def test_gate_execute_normalizes_unified_symbol(tmp_path):
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    ex = FakeExchange({"BTC/USDT:USDT": _uptrend()})
    pf = preflight_step(ex, _settings(), state_dir, memory_dir,
                        now=dt.datetime(2026, 3, 1, tzinfo=UTC), cycle_no=1,
                        http_client=_HttpClient())
    last = pf["briefs"][0]["last_close"]
    # proposal emitted with the UNIFIED symbol must still execute (normalized to raw)
    proposals = [{"symbol": "BTC/USDT:USDT", "direction": "long", "entry": last,
                  "stop": last - 4.0, "take_profits": [last + 8.0], "atr": 2.0,
                  "confidence": 0.7, "rationale": "x"}]
    report = gate_execute_step(ex, _settings(), state_dir, memory_dir,
                               now=dt.datetime(2026, 3, 1, tzinfo=UTC), cycle_no=1,
                               proposals=proposals)
    assert report["opened"] == 1 and report["dropped"] == 0


def test_preflight_attaches_market_context(tmp_path):
    ex = FakeExchange({"BTC/USDT:USDT": _uptrend()})
    ctx = preflight_step(ex, _settings(), tmp_path / "s", tmp_path / "m",
                         now=datetime(2026, 3, 1, tzinfo=UTC), cycle_no=1,
                         http_client=_HttpClient())
    mc = ctx["market_context"]
    assert mc["fear_greed"]["value"] == 30
    assert isinstance(mc["news"], list)
    assert "warnings" in mc
    # the brief now carries derivatives positioning
    assert "long_short_ratio" in ctx["briefs"][0]

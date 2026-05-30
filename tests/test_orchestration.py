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


class _UnionExchange(FakeExchange):
    """FakeExchange that can map a held raw id back to its unified symbol (like ccxt)."""
    def __init__(self, frames, raw_to_unified):
        super().__init__(frames)
        self._r2u = raw_to_unified

    def unified_for_raw(self, raw_id):
        return self._r2u.get(raw_id)

    def symbol_spec(self, symbol):
        from futures_fund.models import MmrBracket, SymbolSpec
        raw = {"BTC/USDT:USDT": "BTCUSDT", "ETH/USDT:USDT": "ETHUSDT"}.get(symbol, "BTCUSDT")
        return SymbolSpec(symbol=raw, tick_size=0.01, step_size=0.001, min_notional=5.0,
                          mmr_brackets=[MmrBracket(notional_floor=0, notional_cap=1_000_000,
                                                   mmr=0.004, maint_amount=0.0, max_leverage=125)])


def test_working_universe_folds_in_held_positions(tmp_path):
    # A position is held in ETH, but this cycle's configured universe is only BTC (the Watcher
    # rotated away). The held symbol MUST be folded in so it is briefed/priced, not stranded.
    from futures_fund.models import MmrBracket  # noqa: F401  (referenced by _UnionExchange)
    from futures_fund.state import Position, save_positions
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    held = Position(symbol="ETHUSDT", direction="long", qty=1.0, entry=100.0, stop=90.0,
                    take_profits=[120.0], leverage=3.0, margin=33.3, liq_price=70.0,
                    opened_cycle=0, opened_ts=dt.datetime(2026, 2, 1, tzinfo=UTC))
    save_positions(state_dir, [held])
    ex = _UnionExchange({"BTC/USDT:USDT": _uptrend(), "ETH/USDT:USDT": _uptrend()},
                        {"ETHUSDT": "ETH/USDT:USDT"})
    ctx = preflight_step(ex, _settings(), state_dir, memory_dir,  # _settings() -> symbols=[BTC]
                         now=dt.datetime(2026, 3, 1, tzinfo=UTC), cycle_no=2,
                         http_client=_HttpClient())
    briefed = {b["exchange_id"] for b in ctx["briefs"]}
    assert "BTCUSDT" in briefed and "ETHUSDT" in briefed  # held ETH folded into the universe


def _seed_holding(tmp_path):
    from futures_fund.state import Position, save_positions
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    held = Position(symbol="ETHUSDT", direction="long", qty=1.0, entry=100.0, stop=90.0,
                    take_profits=[130.0], leverage=3.0, margin=33.3, liq_price=70.0,
                    opened_cycle=1, opened_ts=dt.datetime(2026, 2, 1, tzinfo=UTC))
    save_positions(state_dir, [held])
    ex = _UnionExchange({"BTC/USDT:USDT": _uptrend(), "ETH/USDT:USDT": _uptrend()},
                        {"ETHUSDT": "ETH/USDT:USDT"})
    return state_dir, memory_dir, ex


def test_brief_carries_holding_card_for_open_position(tmp_path):
    from futures_fund.journal import append_decision
    from futures_fund.memory_layout import ensure_memory_layout
    from futures_fund.state import Position, save_positions
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    ensure_memory_layout(memory_dir)
    did = append_decision(memory_dir, {"ts": dt.datetime(2026, 2, 1, tzinfo=UTC), "cycle": 1,
                                       "symbol": "ETHUSDT", "direction": "long", "entry": 100.0,
                                       "stop": 90.0, "rationale": "L2 thesis",
                                       "falsifiable_prediction": "ETH > 999 in 6 cycles"})
    # TP set far above the uptrend so the audit does NOT close it -> it survives to be carded
    save_positions(state_dir, [Position(symbol="ETHUSDT", direction="long", qty=1.0, entry=100.0,
                                        stop=90.0, take_profits=[999.0], leverage=3.0, margin=33.3,
                                        liq_price=70.0, opened_cycle=1,
                                        opened_ts=dt.datetime(2026, 2, 1, tzinfo=UTC),
                                        decision_id=did)])
    ex = _UnionExchange({"BTC/USDT:USDT": _uptrend(), "ETH/USDT:USDT": _uptrend()},
                        {"ETHUSDT": "ETH/USDT:USDT"})
    ctx = preflight_step(ex, _settings(), state_dir, memory_dir,
                         now=dt.datetime(2026, 3, 1, tzinfo=UTC), cycle_no=2,
                         http_client=_HttpClient())
    eth = next(b for b in ctx["briefs"] if b["exchange_id"] == "ETHUSDT")
    assert "holding" in eth
    assert eth["holding"]["original_thesis"] == "L2 thesis"
    assert eth["holding"]["falsifiable_prediction"] == "ETH > 999 in 6 cycles"
    assert "r_progress" in eth["holding"] and "bars_held" in eth["holding"]
    btc = next(b for b in ctx["briefs"] if b["exchange_id"] == "BTCUSDT")
    assert "holding" not in btc  # not held -> no card


def test_holdings_review_keeps_holding_absent_from_new_opens(tmp_path):
    # The Watcher rotated away from ETH and no new ETH proposal exists, but the review said HOLD.
    # With an explicit review present, the holding must NOT be churned closed by absence.
    state_dir, memory_dir, ex = _seed_holding(tmp_path)
    report = gate_execute_step(ex, _settings(), state_dir, memory_dir,
                               now=dt.datetime(2026, 3, 1, tzinfo=UTC), cycle_no=2,
                               proposals=[], management=[{"symbol": "ETHUSDT", "action": "hold"}])
    assert report["closed"] == 0 and report["closed_by_review"] == 0
    assert {p.symbol for p in load_positions(state_dir)} == {"ETHUSDT"}


def test_holdings_review_closes_on_explicit_close(tmp_path):
    state_dir, memory_dir, ex = _seed_holding(tmp_path)
    report = gate_execute_step(ex, _settings(), state_dir, memory_dir,
                               now=dt.datetime(2026, 3, 1, tzinfo=UTC), cycle_no=2,
                               proposals=[], management=[{"symbol": "ETHUSDT", "action": "close"}])
    assert report["closed"] == 1 and report["closed_by_review"] == 1
    assert load_positions(state_dir) == []


def test_holdings_review_trails_stop_in_place(tmp_path):
    state_dir, memory_dir, ex = _seed_holding(tmp_path)
    report = gate_execute_step(
        ex, _settings(), state_dir, memory_dir, now=dt.datetime(2026, 3, 1, tzinfo=UTC),
        cycle_no=2, proposals=[],
        management=[{"symbol": "ETHUSDT", "action": "hold", "new_stop": 95.0}])
    assert report["trailed"] == 1
    pos = load_positions(state_dir)
    assert len(pos) == 1 and pos[0].stop == 95.0  # tightened, never loosened


def test_holdings_review_ignores_loosening_stop(tmp_path):
    state_dir, memory_dir, ex = _seed_holding(tmp_path)
    report = gate_execute_step(  # 80 is BELOW the existing 90 stop on a long -> looser, rejected
        ex, _settings(), state_dir, memory_dir, now=dt.datetime(2026, 3, 1, tzinfo=UTC),
        cycle_no=2, proposals=[],
        management=[{"symbol": "ETHUSDT", "action": "hold", "new_stop": 80.0}])
    assert report["trailed"] == 0
    assert load_positions(state_dir)[0].stop == 90.0


def _btc_long(entry=147.0):  # a valid new-open proposal on BTC (uptrend last close ~147)
    return {"symbol": "BTCUSDT", "direction": "long", "entry": entry, "stop": entry - 4.0,
            "take_profits": [entry + 8.0], "atr": 2.0, "confidence": 0.7, "rationale": "x"}


def test_halt_blocks_new_opens_but_still_closes(tmp_path):
    # Audit fix [2]: a halt tripped (e.g. by the monitor) must block NEW opens at the trade
    # boundary, but explicit holdings CLOSES must still run (a halt should de-risk, not freeze).
    from futures_fund.state import set_halt
    state_dir, memory_dir, ex = _seed_holding(tmp_path)  # held ETH long
    set_halt(state_dir, True, reason="monitor drawdown")
    report = gate_execute_step(ex, _settings(), state_dir, memory_dir,
                               now=dt.datetime(2026, 3, 1, tzinfo=UTC), cycle_no=2,
                               proposals=[_btc_long()],
                               management=[{"symbol": "ETHUSDT", "action": "close"}])
    assert report["halted"] is True
    assert report["opened"] == 0          # new BTC open blocked
    assert report["closed"] == 1          # ETH close still honored
    assert load_positions(state_dir) == []


def test_force_flatten_breaker_closes_all_holdings(tmp_path):
    # Audit fix [3]: a -12% month must FLATTEN the book regardless of the review's verdicts.
    from futures_fund.equity_log import record_equity
    state_dir, memory_dir, ex = _seed_holding(tmp_path)  # held ETH long
    now = dt.datetime(2026, 3, 1, tzinfo=UTC)
    record_equity(state_dir, now - dt.timedelta(days=31), 10_000.0, 0)
    record_equity(state_dir, now, 8_700.0, 1)            # -13% on the month -> force_flatten
    report = gate_execute_step(ex, _settings(), state_dir, memory_dir, now=now, cycle_no=2,
                               proposals=[], management=[{"symbol": "ETHUSDT", "action": "hold"}])
    assert "force_flatten" in report and report["closed"] == 1
    assert load_positions(state_dir) == []  # flattened despite the HOLD verdict


def test_review_never_flips_a_kept_holding(tmp_path):
    # Audit fix [1]: a HOLD + an opposite-direction proposal on the same symbol must NOT stack
    # into a simultaneous long+short. The kept holding is never re-opened/flipped.
    state_dir, memory_dir, ex = _seed_holding(tmp_path)  # held ETH long
    short_eth = {"symbol": "ETHUSDT", "direction": "short", "entry": 147.0, "stop": 151.0,
                 "take_profits": [140.0], "atr": 2.0, "confidence": 0.7, "rationale": "x"}
    report = gate_execute_step(ex, _settings(), state_dir, memory_dir,
                               now=dt.datetime(2026, 3, 1, tzinfo=UTC), cycle_no=2,
                               proposals=[short_eth],
                               management=[{"symbol": "ETHUSDT", "action": "hold"}])
    pos = load_positions(state_dir)
    assert len(pos) == 1 and pos[0].symbol == "ETHUSDT" and pos[0].direction == "long"
    assert report["opened"] == 0  # the counter-direction proposal was NOT opened


def test_explicit_close_then_same_direction_reopen(tmp_path):
    # Audit fix [5]: CLOSE a holding AND re-propose the same direction -> close+reopen (one net).
    state_dir, memory_dir, ex = _seed_holding(tmp_path)  # held ETH long entry 100
    reopen = {"symbol": "ETHUSDT", "direction": "long", "entry": 147.0, "stop": 143.0,
              "take_profits": [160.0], "atr": 2.0, "confidence": 0.7, "rationale": "x"}
    report = gate_execute_step(ex, _settings(), state_dir, memory_dir,
                               now=dt.datetime(2026, 3, 1, tzinfo=UTC), cycle_no=2,
                               proposals=[reopen],
                               management=[{"symbol": "ETHUSDT", "action": "close"}])
    assert report["closed"] == 1 and report["opened"] == 1 and report["closed_by_review"] == 1
    pos = load_positions(state_dir)
    assert len(pos) == 1 and pos[0].direction == "long" and pos[0].entry > 120  # the NEW long


def test_malformed_proposal_dropped_others_proceed(tmp_path):
    # Audit fix [4]: one malformed proposal must not abort the gate; good opens still execute.
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    ex = _UnionExchange({"BTC/USDT:USDT": _uptrend()}, {})
    bad = {"symbol": "BTCUSDT", "direction": "long", "entry": 100.0, "stop": 110.0,  # stop>entry
           "take_profits": [120.0], "atr": 2.0, "confidence": 0.7, "rationale": "inverted"}
    report = gate_execute_step(ex, _settings(), state_dir, memory_dir,
                               now=dt.datetime(2026, 3, 1, tzinfo=UTC), cycle_no=1,
                               proposals=[_btc_long(), bad], management=None)
    assert report["opened"] == 1 and report["dropped"] >= 1


def test_falsifiable_prediction_is_journaled_at_entry(tmp_path):
    # The RM's falsifiable_prediction must persist into the decision journal so the later
    # HOLD/CLOSE review can test it (previously it was dropped -> holding card showed null).
    from futures_fund.journal import read_open_decisions
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    ex = FakeExchange({"BTC/USDT:USDT": _uptrend()})
    pf = preflight_step(ex, _settings(), state_dir, memory_dir,
                        now=dt.datetime(2026, 3, 1, tzinfo=UTC), cycle_no=1,
                        http_client=_HttpClient())
    last = pf["briefs"][0]["last_close"]
    proposals = [{"symbol": "BTCUSDT", "direction": "long", "entry": last, "stop": last - 4.0,
                  "take_profits": [last + 8.0], "atr": 2.0, "confidence": 0.7, "rationale": "x",
                  "falsifiable_prediction": "BTC makes a higher high within 6 cycles"}]
    gate_execute_step(ex, _settings(), state_dir, memory_dir,
                      now=dt.datetime(2026, 3, 1, tzinfo=UTC), cycle_no=1, proposals=proposals)
    decs = read_open_decisions(memory_dir)
    assert len(decs) == 1
    assert decs[0]["falsifiable_prediction"] == "BTC makes a higher high within 6 cycles"


def test_empty_universe_stands_down(tmp_path):
    # Audit fix [11]: an empty universe (failed scan / degenerate picks) must stand down, not crash.
    from futures_fund.config import Settings
    ex = _UnionExchange({}, {})
    s = Settings(account_size_usdt=10_000.0, symbols=[], timeframe="4h")
    report = gate_execute_step(ex, s, tmp_path / "s", tmp_path / "m",
                               now=dt.datetime(2026, 3, 1, tzinfo=UTC), cycle_no=1,
                               proposals=[_btc_long()], management=None)
    assert report.get("stood_down") is True and report["opened"] == 0


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

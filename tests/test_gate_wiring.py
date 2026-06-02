"""Wiring of regime arbiter (#1) + trigger orders (#2) into the gate. Reuses the FakeExchange
harness from test_orchestration. Asserts: shorts dropped under a prohibited regime, armed triggers
fire into proposals, unfired triggers stay armed, regime_state emitted by preflight."""
import datetime as dt
from datetime import UTC

from futures_fund.contracts import AgentProposal
from futures_fund.orchestration import gate_execute_step, preflight_step
from futures_fund.pending_orders import PendingOrder, load_pending_orders, save_pending_orders
from futures_fund.state import load_positions
from tests.test_orchestration import FakeExchange, _HttpClient, _settings, _uptrend

NOW = dt.datetime(2026, 3, 1, tzinfo=UTC)


def _pf(state_dir, memory_dir, ex):
    return preflight_step(ex, _settings(), state_dir, memory_dir, now=NOW, cycle_no=1,
                          http_client=_HttpClient())


def _regime(label):
    return {"regime": label, "confirmed": label == "risk_off",
            "drivers": {"quorum_met": True, "deterministic_regime": label}}


def test_preflight_emits_regime_state(tmp_path):
    ex = FakeExchange({"BTC/USDT:USDT": _uptrend()})
    ctx = _pf(tmp_path / "s", tmp_path / "m", ex)
    assert "regime_state" in ctx
    assert "regime" in ctx["regime_state"] and "confirmed" in ctx["regime_state"]
    assert "shorts_permitted" not in ctx["regime_state"]  # removed: shorts are never gated


def test_preflight_emits_exposure(tmp_path):
    # market-neutral mandate: preflight surfaces the dollar-neutral book exposure for the agents
    ex = FakeExchange({"BTC/USDT:USDT": _uptrend()})
    ctx = _pf(tmp_path / "s", tmp_path / "m", ex)
    assert "exposure" in ctx
    for k in ("gross_long", "gross_short", "net", "tilt", "long_share"):
        assert k in ctx["exposure"]


def test_gate_report_carries_post_trade_exposure(tmp_path):
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    ex = FakeExchange({"BTC/USDT:USDT": _uptrend()})
    last = _pf(state_dir, memory_dir, ex)["briefs"][0]["last_close"]
    report = gate_execute_step(ex, _settings(), state_dir, memory_dir, now=NOW, cycle_no=1,
                               proposals=[_long(last)], regime_state=_regime("risk_on"))
    assert report["opened"] == 1 and "exposure" in report
    assert report["exposure"]["gross_long"] > 0 and report["exposure"]["n_long"] == 1


def _short(last):
    return AgentProposal(symbol="BTCUSDT", direction="short", entry=last, stop=last + 4.0,
                         take_profits=[last - 8.0], atr=2.0, confidence=0.7, rationale="x").model_dump()


def _long(last):
    return AgentProposal(symbol="BTCUSDT", direction="long", entry=last, stop=last - 4.0,
                         take_profits=[last + 8.0], atr=2.0, confidence=0.7, rationale="x").model_dump()


def test_gate_converts_counter_regime_short_to_trigger(tmp_path):
    # a SHORT while regime=risk_on is COUNTER-regime -> converted to a confirmation trigger, NOT
    # dropped and NOT opened at market. (No more shorts drop-filter.)
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    ex = FakeExchange({"BTC/USDT:USDT": _uptrend()})
    last = _pf(state_dir, memory_dir, ex)["briefs"][0]["last_close"]
    report = gate_execute_step(ex, _settings(), state_dir, memory_dir, now=NOW, cycle_no=1,
                               proposals=[_short(last)], regime_state=_regime("risk_on"))
    assert report["opened"] == 0 and report["counter_regime_triggered"] == 1
    assert report["triggers_armed"] == 1 and report["market_entries"] == 0


def test_gate_converts_counter_regime_long_to_trigger(tmp_path):
    # the SYMMETRIC mirror: a LONG while regime=risk_off is COUNTER-regime -> also converted.
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    ex = FakeExchange({"BTC/USDT:USDT": _uptrend()})
    last = _pf(state_dir, memory_dir, ex)["briefs"][0]["last_close"]
    report = gate_execute_step(ex, _settings(), state_dir, memory_dir, now=NOW, cycle_no=1,
                               proposals=[_long(last)], regime_state=_regime("risk_off"))
    assert report["opened"] == 0 and report["counter_regime_triggered"] == 1


def test_gate_takes_with_regime_long_at_market(tmp_path):
    # a LONG while regime=risk_on (or mixed) is WITH-regime -> opens at market, no conversion.
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    ex = FakeExchange({"BTC/USDT:USDT": _uptrend()})
    last = _pf(state_dir, memory_dir, ex)["briefs"][0]["last_close"]
    report = gate_execute_step(ex, _settings(), state_dir, memory_dir, now=NOW, cycle_no=1,
                               proposals=[_long(last)], regime_state=_regime("risk_on"))
    assert report["opened"] == 1 and report["counter_regime_triggered"] == 0


def test_gate_mixed_regime_takes_both_at_market(tmp_path):
    # in 'mixed' there is no directional read -> NEITHER side is counter-regime; both go at market
    # (symmetric: a short is as tradable as a long).
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    ex = FakeExchange({"BTC/USDT:USDT": _uptrend()})
    last = _pf(state_dir, memory_dir, ex)["briefs"][0]["last_close"]
    report = gate_execute_step(ex, _settings(), state_dir, memory_dir, now=NOW, cycle_no=1,
                               proposals=[_short(last)], regime_state=_regime("mixed"))
    assert report["counter_regime_triggered"] == 0 and report["market_entries"] == 1


def test_gate_failclosed_untrustworthy_regime_confirms_both(tmp_path):
    # no quorum (untrustworthy read) -> BOTH a long and a short must confirm (symmetric fail-closed).
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    ex = FakeExchange({"BTC/USDT:USDT": _uptrend()})
    last = _pf(state_dir, memory_dir, ex)["briefs"][0]["last_close"]
    bad = {"regime": "mixed", "drivers": {"quorum_met": False}}
    report = gate_execute_step(ex, _settings(), state_dir, memory_dir, now=NOW, cycle_no=1,
                               proposals=[_long(last), _short(last)], regime_state=bad)
    assert report["opened"] == 0 and report["counter_regime_triggered"] == 2


def test_gate_missing_context_sentinel_fails_closed(tmp_path):
    # FIX #1/#5: the gate_execute_cli degraded sentinel (substituted when context.json is missing)
    # must route BOTH directions through confirmation -> no naked market entry on an unread tape.
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    ex = FakeExchange({"BTC/USDT:USDT": _uptrend()})
    last = _pf(state_dir, memory_dir, ex)["briefs"][0]["last_close"]
    sentinel = {"regime": "mixed", "confirmed": False,
                "drivers": {"quorum_met": False, "degraded": ["context_missing"]}}
    report = gate_execute_step(ex, _settings(), state_dir, memory_dir, now=NOW, cycle_no=1,
                               proposals=[_short(last)], regime_state=sentinel)
    assert report["opened"] == 0 and report["counter_regime_triggered"] == 1


def test_gate_reconfirms_counter_regime_limit_fill(tmp_path):
    # FIX #2: a counter-regime LIMIT_ENTRY fill (a TOUCH, not a confirmed break) is re-routed through
    # confirmation, not opened at market. The short fired on the up-touch but risk_on makes it
    # counter-regime -> converted to a stop_entry, not a naked market short.
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    ex = FakeExchange({"BTC/USDT:USDT": _uptrend()})
    last = _pf(state_dir, memory_dir, ex)["briefs"][0]["last_close"]
    save_pending_orders(state_dir, [PendingOrder(
        symbol="BTCUSDT", direction="short", kind="limit_entry", trigger_level=last,
        stop=last + 5.0, take_profits=[last - 10.0], atr=2.0, created_cycle=0, expires_cycle=9)])
    report = gate_execute_step(ex, _settings(), state_dir, memory_dir, now=NOW, cycle_no=1,
                               proposals=[], regime_state=_regime("risk_on"))
    assert report["triggers_fired"] == 1                                   # it DID fire (touch)
    assert report["opened"] == 0 and report["counter_regime_triggered"] == 1  # but got re-confirmed


def test_gate_counter_regime_trigger_not_clobbered_by_trader_trigger(tmp_path):
    # FIX #6: a Trader trigger sharing (symbol, direction, kind) with the auto-armed counter-regime
    # safety trigger must NOT silently clobber it — the safety conversion wins; collision counted.
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    ex = FakeExchange({"BTC/USDT:USDT": _uptrend()})
    last = _pf(state_dir, memory_dir, ex)["briefs"][0]["last_close"]
    trader_trigger = {"symbol": "BTCUSDT", "direction": "short", "kind": "stop_entry",
                      "trigger_level": last - 5.0, "stop": last + 3.0,
                      "take_profits": [last - 15.0], "atr": 2.0, "expires_cycle": 4}
    report = gate_execute_step(ex, _settings(), state_dir, memory_dir, now=NOW, cycle_no=1,
                               proposals=[_short(last)], regime_state=_regime("risk_on"),
                               triggers=[trader_trigger])
    assert report["counter_regime_triggered"] == 1 and report["armed_collisions"] == 1
    assert report["triggers_armed"] == 1 and len(load_pending_orders(state_dir)) == 1


def test_gate_fires_armed_long_trigger(tmp_path):
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    ex = FakeExchange({"BTC/USDT:USDT": _uptrend()})
    last = _pf(state_dir, memory_dir, ex)["briefs"][0]["last_close"]
    # stop_entry long fires when the latest 4h CLOSE > trigger; uptrend close is `last`
    save_pending_orders(state_dir, [PendingOrder(
        symbol="BTCUSDT", direction="long", kind="stop_entry", trigger_level=last - 2.0,
        stop=last - 8.0, take_profits=[last + 10.0], atr=2.0, created_cycle=1, expires_cycle=5)])
    report = gate_execute_step(ex, _settings(), state_dir, memory_dir, now=NOW, cycle_no=1,
                               proposals=[])
    assert report["triggers_fired"] == 1 and report["opened"] == 1
    assert load_pending_orders(state_dir) == []  # fired order consumed from the store
    assert load_positions(state_dir)[0].symbol == "BTCUSDT"


def test_gate_report_surfaces_news_fold_signal(tmp_path):
    # FIX 6: the gate report must echo whether the Phase 4.6 news fold engaged, so a silently
    # skipped reclassify is distinguishable from a folded cycle.
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    ex = FakeExchange({"BTC/USDT:USDT": _uptrend()})
    _pf(state_dir, memory_dir, ex)
    folded = gate_execute_step(ex, _settings(), state_dir, memory_dir, now=NOW, cycle_no=1,
                               proposals=[], regime_state={
                                   "regime": "risk_off", "confirmed": False, "shorts_permitted": False,
                                   "score": -0.75, "candle": NOW.isoformat(), "cycle_no": 1,
                                   "drivers": {"news_risk_off": True, "degraded": []}})
    assert folded["news_risk_off"] is True and folded["news_folded"] is True
    assert folded["regime_degraded"] == []
    # a degraded (un-folded) cycle: news_risk_off None, news_flag_missing still present
    degraded = gate_execute_step(ex, _settings(), state_dir, memory_dir, now=NOW, cycle_no=1,
                                 proposals=[], regime_state={
                                     "regime": "mixed", "confirmed": False, "shorts_permitted": False,
                                     "score": 0.0, "candle": NOW.isoformat(), "cycle_no": 1,
                                     "drivers": {"news_risk_off": None, "degraded": ["news_flag_missing"]}})
    assert degraded["news_risk_off"] is None and degraded["news_folded"] is False
    assert "news_flag_missing" in degraded["regime_degraded"]


def _armed_btc_short(state_dir, last):
    save_pending_orders(state_dir, [PendingOrder(
        symbol="BTCUSDT", direction="short", kind="stop_entry", trigger_level=last - 50.0,
        stop=last - 40.0, take_profits=[last - 60.0], atr=2.0, created_cycle=1, expires_cycle=99)])


def test_gate_cancels_armed_trigger_via_directive(tmp_path):
    # the TEAM retires a decayed trigger through the normal flow (cancel_triggers in proposals.json),
    # NOT a manual store edit. A matching armed order is removed before persistence + counted.
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    ex = FakeExchange({"BTC/USDT:USDT": _uptrend()})
    last = _pf(state_dir, memory_dir, ex)["briefs"][0]["last_close"]
    _armed_btc_short(state_dir, last)
    report = gate_execute_step(ex, _settings(), state_dir, memory_dir, now=NOW, cycle_no=2,
                               proposals=[], cancel_triggers=[{"symbol": "BTCUSDT"}])
    assert report["triggers_canceled"] == 1 and report["triggers_remaining"] == 0
    assert load_pending_orders(state_dir) == []  # retired through the gate, not by hand


def test_gate_cancel_non_matching_symbol_keeps_trigger(tmp_path):
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    ex = FakeExchange({"BTC/USDT:USDT": _uptrend()})
    last = _pf(state_dir, memory_dir, ex)["briefs"][0]["last_close"]
    _armed_btc_short(state_dir, last)
    report = gate_execute_step(ex, _settings(), state_dir, memory_dir, now=NOW, cycle_no=2,
                               proposals=[], cancel_triggers=[{"symbol": "ZECUSDT"}])
    assert report["triggers_canceled"] == 0 and report["triggers_remaining"] == 1
    assert len(load_pending_orders(state_dir)) == 1


def test_gate_cancel_beats_same_cycle_counter_regime_rearm(tmp_path):
    # cancel is AUTHORITATIVE: a counter-regime SHORT proposal the gate auto-arms as a stop_entry must
    # NOT survive if the team also cancels that key the same cycle (review fix — was silently re-armed).
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    ex = FakeExchange({"BTC/USDT:USDT": _uptrend()})
    last = _pf(state_dir, memory_dir, ex)["briefs"][0]["last_close"]
    report = gate_execute_step(ex, _settings(), state_dir, memory_dir, now=NOW, cycle_no=2,
                               proposals=[_short(last)], regime_state=_regime("risk_on"),
                               cancel_triggers=[{"symbol": "BTCUSDT", "direction": "short", "kind": "stop_entry"}])
    assert report["counter_regime_triggered"] == 1   # it WAS converted
    assert report["triggers_armed"] == 0             # but cancel stripped it before the save
    assert report["triggers_canceled"] >= 1
    assert load_pending_orders(state_dir) == []       # not in the persisted store


def test_gate_cancel_beats_same_cycle_fire(tmp_path):
    # a canceled trigger that ALSO fires this cycle must NOT open (cancel wins over a confirmed break;
    # review fix — was opening anyway, contradicting "never let a stale trigger ride into a fire").
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    ex = FakeExchange({"BTC/USDT:USDT": _uptrend()})
    last = _pf(state_dir, memory_dir, ex)["briefs"][0]["last_close"]
    save_pending_orders(state_dir, [PendingOrder(
        symbol="BTCUSDT", direction="short", kind="stop_entry", trigger_level=last + 10.0,
        stop=last + 20.0, take_profits=[last - 10.0], atr=2.0, created_cycle=1, expires_cycle=99)])
    report = gate_execute_step(ex, _settings(), state_dir, memory_dir, now=NOW, cycle_no=2,
                               proposals=[], cancel_triggers=[{"symbol": "BTCUSDT"}])
    assert report["opened"] == 0 and report["triggers_fired"] == 0  # the fire was retired, not opened
    assert report["triggers_canceled"] == 1
    assert load_pending_orders(state_dir) == []


def test_gate_cancel_respects_direction_filter(tmp_path):
    # cancel {symbol BTCUSDT, direction long} must NOT retire an armed BTCUSDT SHORT
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    ex = FakeExchange({"BTC/USDT:USDT": _uptrend()})
    last = _pf(state_dir, memory_dir, ex)["briefs"][0]["last_close"]
    _armed_btc_short(state_dir, last)
    report = gate_execute_step(ex, _settings(), state_dir, memory_dir, now=NOW, cycle_no=2,
                               proposals=[], cancel_triggers=[{"symbol": "BTCUSDT", "direction": "long"}])
    assert report["triggers_canceled"] == 0 and len(load_pending_orders(state_dir)) == 1


def test_gate_leaves_unfired_trigger_armed(tmp_path):
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    ex = FakeExchange({"BTC/USDT:USDT": _uptrend()})
    last = _pf(state_dir, memory_dir, ex)["briefs"][0]["last_close"]
    # stop_entry short fires on close < trigger; trigger far below -> never fires this cycle
    save_pending_orders(state_dir, [PendingOrder(
        symbol="BTCUSDT", direction="short", kind="stop_entry", trigger_level=last - 50.0,
        stop=last - 40.0, take_profits=[last - 60.0], atr=2.0, created_cycle=1, expires_cycle=99)])
    report = gate_execute_step(ex, _settings(), state_dir, memory_dir, now=NOW, cycle_no=1,
                               proposals=[])  # no `triggers` key -> store not wiped
    assert report["triggers_fired"] == 0 and report["triggers_remaining"] == 1
    assert len(load_pending_orders(state_dir)) == 1  # still armed


def test_reduce_is_honored_on_halt(tmp_path):
    # a reduce is risk-DECREASING, so like a close it must still run under HALT
    import datetime as dt
    from futures_fund.orchestration import gate_execute_step
    from futures_fund.state import load_positions, set_halt
    from tests.test_orchestration import _seed_holding, _settings
    state_dir, memory_dir, ex = _seed_holding(tmp_path)
    set_halt(state_dir, True, reason="test halt")  # set_halt(state_dir, halt, reason="") — state.py:90
    report = gate_execute_step(
        ex, _settings(), state_dir, memory_dir, now=dt.datetime(2026, 3, 1, tzinfo=dt.UTC),
        cycle_no=2, proposals=[],
        management=[{"symbol": "ETHUSDT", "action": "reduce", "reduce_fraction": 0.5}])
    assert report["halted"] is True
    assert report["reduced"] == 1 and load_positions(state_dir)[0].qty == 0.5  # trim ran on halt

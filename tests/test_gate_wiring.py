"""Wiring of regime arbiter (#1) + trigger orders (#2) into the gate. Reuses the FakeExchange
harness from test_orchestration. Asserts: shorts dropped under a prohibited regime, armed triggers
fire into proposals, unfired triggers stay armed, regime_state emitted by preflight."""
import datetime as dt
from datetime import UTC

from futures_fund.contracts import AgentProposal
from futures_fund.orchestration import gate_execute_step, preflight_step
from futures_fund.pending_orders import PendingOrder, load_pending_orders, save_pending_orders
from futures_fund.state import load_positions
from tests.test_orchestration import (
    FakeExchange,
    _HttpClient,
    _settings,
    _UnionExchange,
    _uptrend,
)

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


def test_gate_reroutes_misrouted_stop_entry_proposal_to_triggers(tmp_path):
    # REGRESSION (cy70): a resting stop_entry order MISROUTED into `proposals` (the market-intent
    # channel) must be RE-ROUTED to triggers and armed as a resting order — never silently mangled
    # (a with-regime one was opened as a no-op market entry; a counter-regime one was dropped). The
    # reroute is surfaced LOUD via report['proposals_rerouted'] + a warning, and every downstream
    # gate guard (audit, crypto-only, RR floor, geometry) still applies.
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    ex = FakeExchange({"BTC/USDT:USDT": _uptrend()})
    last = _pf(state_dir, memory_dir, ex)["briefs"][0]["last_close"]
    # trigger below last => a short stop_entry does NOT fire on this bar -> arms. RR = 12/5 = 2.4.
    prop = {"symbol": "BTCUSDT", "direction": "short", "kind": "stop_entry",
            "trigger_level": last - 10.0, "stop": last - 5.0, "take_profits": [last - 22.0],
            "atr": 2.0, "confidence": 0.7, "rationale": "misrouted resting order"}
    report = gate_execute_step(ex, _settings(), state_dir, memory_dir, now=NOW, cycle_no=1,
                               proposals=[prop], regime_state=_regime("risk_off"))
    assert report["proposals_rerouted"] == 1
    assert report["opened"] == 0 and report["market_entries"] == 0  # NOT mangled as a market entry
    assert report["triggers_armed"] == 1                             # armed as a resting trigger
    assert any("REROUTED" in w for w in report.get("warnings", []))  # surfaced LOUD (Rule 6)
    orders = load_pending_orders(state_dir)                          # and persisted to the store
    assert any(o.symbol == "BTCUSDT" and o.kind == "stop_entry" for o in orders)


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


def test_gate_trigger_price_synonym_is_armed_not_dropped(tmp_path):
    # cy84 bug: the Trader emitted a stop_entry with `trigger_price` (not `trigger_level`); the
    # PendingOrder ingestion raised and the trigger was SILENTLY dropped (SOL never armed). The
    # arming loop now normalizes the synonym so the resting order is honored.
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    ex = FakeExchange({"BTC/USDT:USDT": _uptrend()})
    last = _pf(state_dir, memory_dir, ex)["briefs"][0]["last_close"]
    # with-regime long breakout above mark, level under `trigger_price` not `trigger_level`
    trig = {"symbol": "BTCUSDT", "direction": "long", "kind": "stop_entry",
            "trigger_price": last + 10.0, "stop": last + 5.0, "take_profits": [last + 30.0],
            "atr": 2.0, "expires_cycle": 4}
    report = gate_execute_step(ex, _settings(), state_dir, memory_dir, now=NOW, cycle_no=1,
                               proposals=[], regime_state=_regime("risk_on"), triggers=[trig])
    assert report["triggers_armed"] == 1 and report.get("triggers_malformed_dropped", 0) == 0
    orders = load_pending_orders(state_dir)
    assert any(o.symbol == "BTCUSDT" and o.trigger_level == last + 10.0 for o in orders)


def test_gate_malformed_trigger_dropped_loud(tmp_path):
    # a trigger that cannot be validated (no usable level, missing required fields) must be dropped
    # LOUD — counted + warned — never silently swallowed (Rule 6 fail-loud).
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    ex = FakeExchange({"BTC/USDT:USDT": _uptrend()})
    last = _pf(state_dir, memory_dir, ex)["briefs"][0]["last_close"]
    bad = {"symbol": "BTCUSDT", "direction": "long", "kind": "stop_entry"}  # no level/stop/TP
    report = gate_execute_step(ex, _settings(), state_dir, memory_dir, now=NOW, cycle_no=1,
                               proposals=[], regime_state=_regime("risk_on"), triggers=[bad])
    assert report["triggers_malformed_dropped"] == 1 and report["triggers_armed"] == 0
    assert any("malformed" in w for w in report.get("warnings", []))
    assert last  # last_close fixture used (silences unused-var lint)


def test_gate_fires_armed_long_trigger(tmp_path):
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    ex = FakeExchange({"BTC/USDT:USDT": _uptrend()})
    last = _pf(state_dir, memory_dir, ex)["briefs"][0]["last_close"]
    # stop_entry long fires when the latest 4h CLOSE > trigger; uptrend close is `last`. The trigger
    # sits BELOW the narrow ±0.2 bar (a clean gap), so the gap-honest fill is the bar OPEN (~last);
    # RR = 10/4 at that realistic fill clears the floor.
    save_pending_orders(state_dir, [PendingOrder(
        symbol="BTCUSDT", direction="long", kind="stop_entry", trigger_level=last - 2.0,
        stop=last - 4.0, take_profits=[last + 10.0], atr=2.0, created_cycle=1, expires_cycle=5)])
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


# --- OI-confirmation gate at the GATE level: an opted-in (require_oi_rising) armed trigger fires on
# its price-break ONLY IF OI is rising at fire time; spent/missing OI HOLDS it armed (fail-safe).
class _OiRisingEx(FakeExchange):
    def open_interest_history(self, symbol, period="4h", limit=200):
        import pandas as pd
        return pd.DataFrame({
            "timestamp": pd.date_range("2026-01-01", periods=6, freq="4h", tz="UTC"),
            "oi_amount": [1.0] * 6,
            "oi_value": [1.00e7, 1.02e7, 1.04e7, 1.06e7, 1.08e7, 1.10e7]})


class _OiBleedingEx(FakeExchange):
    def open_interest_history(self, symbol, period="4h", limit=200):
        import pandas as pd
        return pd.DataFrame({
            "timestamp": pd.date_range("2026-01-01", periods=6, freq="4h", tz="UTC"),
            "oi_amount": [1.0] * 6,
            "oi_value": [1.10e7, 1.08e7, 1.06e7, 1.04e7, 1.02e7, 1.00e7]})


class _OiFeedDownEx(FakeExchange):
    def open_interest_history(self, symbol, period="4h", limit=200):
        raise RuntimeError("oi feed down")


def _armed_oi_short(last, require_oi_rising):
    # trigger above the last completed close so the price-break (close < trigger) is satisfied;
    # the OI-gate then decides fire vs hold. The narrow _uptrend bar sits BELOW the trigger (a gap),
    # so the gap-honest fill is the bar OPEN (~last); RR = 20/8 = 2.5 at that fill clears the gate.
    return PendingOrder(symbol="BTCUSDT", direction="short", kind="stop_entry",
                        trigger_level=last + 5.0, stop=last + 8.0, take_profits=[last - 20.0],
                        atr=2.0, require_oi_rising=require_oi_rising,
                        created_cycle=0, expires_cycle=9)


def test_gate_oi_gate_fires_when_oi_rising(tmp_path):
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    ex = _OiRisingEx({"BTC/USDT:USDT": _uptrend()})
    last = _pf(state_dir, memory_dir, ex)["briefs"][0]["last_close"]
    save_pending_orders(state_dir, [_armed_oi_short(last, True)])
    report = gate_execute_step(ex, _settings(), state_dir, memory_dir, now=NOW, cycle_no=1,
                               proposals=[], regime_state=_regime("risk_off"))
    assert report["triggers_fired"] == 1 and report["opened"] == 1


def test_gate_oi_gate_holds_when_oi_bleeding(tmp_path):
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    ex = _OiBleedingEx({"BTC/USDT:USDT": _uptrend()})
    last = _pf(state_dir, memory_dir, ex)["briefs"][0]["last_close"]
    save_pending_orders(state_dir, [_armed_oi_short(last, True)])
    report = gate_execute_step(ex, _settings(), state_dir, memory_dir, now=NOW, cycle_no=1,
                               proposals=[], regime_state=_regime("risk_off"))
    assert report["triggers_fired"] == 0 and report["triggers_remaining"] == 1  # break held, armed


def test_gate_oi_gate_holds_on_feed_down(tmp_path):
    # OI feed raises -> oi_change None -> fail-closed: the opted-in break does NOT fire (no phantom)
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    ex = _OiFeedDownEx({"BTC/USDT:USDT": _uptrend()})
    last = _pf(state_dir, memory_dir, ex)["briefs"][0]["last_close"]
    save_pending_orders(state_dir, [_armed_oi_short(last, True)])
    report = gate_execute_step(ex, _settings(), state_dir, memory_dir, now=NOW, cycle_no=1,
                               proposals=[], regime_state=_regime("risk_off"))
    assert report["triggers_fired"] == 0 and report["triggers_remaining"] == 1


def test_gate_oi_gate_default_off_fires_regardless_of_oi(tmp_path):
    # a DEFAULT trigger (require_oi_rising=False) fires on the break even with bleeding OI = today's
    # behavior; the OI feed is never even consulted for it (inert on the hot path).
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    ex = _OiBleedingEx({"BTC/USDT:USDT": _uptrend()})
    last = _pf(state_dir, memory_dir, ex)["briefs"][0]["last_close"]
    save_pending_orders(state_dir, [_armed_oi_short(last, False)])
    report = gate_execute_step(ex, _settings(), state_dir, memory_dir, now=NOW, cycle_no=1,
                               proposals=[], regime_state=_regime("risk_off"))
    assert report["triggers_fired"] == 1 and report["opened"] == 1


def test_gate_no_oi_fetch_when_no_trigger_opts_in(tmp_path):
    # must-fix #2: the OI feed must NOT be hit by the GATE when no armed trigger opts in (the
    # feature is inert on the execution hot path by default — no latency/rate-limit regression).
    class _CountingEx(FakeExchange):
        oi_calls = 0
        def open_interest_history(self, *a, **k):
            type(self).oi_calls += 1
            return super().open_interest_history(*a, **k)
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    ex = _CountingEx({"BTC/USDT:USDT": _uptrend()})
    last = _pf(state_dir, memory_dir, ex)["briefs"][0]["last_close"]
    save_pending_orders(state_dir, [_armed_oi_short(last, False)])   # default: NOT opted in
    _CountingEx.oi_calls = 0   # ignore preflight's brief OI reads; count only the gate
    gate_execute_step(ex, _settings(), state_dir, memory_dir, now=NOW, cycle_no=1,
                      proposals=[], regime_state=_regime("risk_off"))
    assert _CountingEx.oi_calls == 0


def test_gate_fetches_oi_only_for_opted_in_symbol(tmp_path):
    # the opted-in symbol's OI IS fetched at fire time (the other half of must-fix #2's gating).
    class _CountingEx(_OiRisingEx):
        oi_calls = 0
        def open_interest_history(self, *a, **k):
            type(self).oi_calls += 1
            return super().open_interest_history(*a, **k)
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    ex = _CountingEx({"BTC/USDT:USDT": _uptrend()})
    last = _pf(state_dir, memory_dir, ex)["briefs"][0]["last_close"]
    save_pending_orders(state_dir, [_armed_oi_short(last, True)])   # opted in
    _CountingEx.oi_calls = 0
    gate_execute_step(ex, _settings(), state_dir, memory_dir, now=NOW, cycle_no=1,
                      proposals=[], regime_state=_regime("risk_off"))
    assert _CountingEx.oi_calls >= 1


# ---- Stale-trigger auto-revalidation: a prior-armed stop_entry whose swing crossed PAST its level
# is auto-canceled through the gate (never opened, never persisted) — symmetric+fail-safe+reported.

def _armed(direction, trigger, stop, tps, atr=2.0, anchor=None, kind="stop_entry"):
    return PendingOrder(symbol="BTCUSDT", direction=direction, kind=kind, trigger_level=trigger,
                        stop=stop, take_profits=tps, atr=atr, anchor_swing=anchor,
                        created_cycle=0, expires_cycle=9)


def test_gate_does_not_stale_cancel_a_fired_short(tmp_path):
    # cy66 BCH faithfulness: a short whose 20-bar swing_low has drifted below the trigger is
    # geometrically "stale" — BUT it FIRES this bar (close < trigger), i.e. price traded through the
    # level, which a LIVE resting sell-stop would have FILLED. A fired trigger reproduces that live
    # fill and must NOT be auto-canceled; only UN-FIRED drifted triggers are retired. (Synthetic
    # fixture: trigger above the doji bar; the point is the FIRED-exemption, not the fill geometry.)
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    ex = FakeExchange({"BTC/USDT:USDT": _uptrend()})
    last = _pf(state_dir, memory_dir, ex)["briefs"][0]["last_close"]
    save_pending_orders(state_dir, [_armed("short", trigger=last + 4.0, stop=last + 7.0,
                                           tps=[last - 20.0], anchor=last + 8.0)])
    report = gate_execute_step(ex, _settings(), state_dir, memory_dir, now=NOW, cycle_no=1,
                               proposals=[], regime_state=_regime("risk_off"))
    assert report["auto_canceled_stale"] == 0            # fired trigger is EXEMPT from stale-cancel
    assert report["triggers_fired"] == 1 and report["opened"] == 1
    assert load_pending_orders(state_dir) == []          # consumed from the store, not re-armed


def test_gate_does_not_stale_cancel_a_fired_long_mirror(tmp_path):
    # mirror: a long whose swing_high drifted above the trigger is geometrically stale, yet it
    # FIRES (close > trigger) = a live buy-stop fill -> opens, not canceled (no long/short bias).
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    ex = FakeExchange({"BTC/USDT:USDT": _uptrend()})
    last = _pf(state_dir, memory_dir, ex)["briefs"][0]["last_close"]
    save_pending_orders(state_dir, [_armed("long", trigger=last - 10.0, stop=last - 11.0,
                                           tps=[last + 30.0], anchor=last - 12.0)])
    report = gate_execute_step(ex, _settings(), state_dir, memory_dir, now=NOW, cycle_no=1,
                               proposals=[], regime_state=_regime("risk_on"))
    assert report["auto_canceled_stale"] == 0 and report["triggers_fired"] == 1
    assert report["opened"] == 1
    assert load_pending_orders(state_dir) == []          # consumed from the store, not re-armed


def test_gate_auto_cancels_stale_unfired_short(tmp_path):
    # the KEPT protection: an UN-FIRED short (close stayed ABOVE the trigger — no break this bar)
    # whose anchored support has since drifted BELOW the trigger is retired so it can't fire LATE on
    # a mid-bounce re-touch next cycle. Only un-fired (remaining) triggers are revalidated now.
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    ex = FakeExchange({"BTC/USDT:USDT": _uptrend()})            # close ~147.2, swing_low ~131.8
    last = _pf(state_dir, memory_dir, ex)["briefs"][0]["last_close"]
    # trigger below the close (close > trigger => no fire), support drifted below it => stale
    save_pending_orders(state_dir, [_armed("short", trigger=last - 7.0, stop=last - 1.0,
                                           tps=[last - 27.0], anchor=last - 6.0)])
    report = gate_execute_step(ex, _settings(), state_dir, memory_dir, now=NOW, cycle_no=1,
                               proposals=[], regime_state=_regime("risk_off"))
    assert report["auto_canceled_stale"] == 1
    assert report["triggers_fired"] == 0 and report["opened"] == 0
    assert load_pending_orders(state_dir) == []          # not persisted -> team re-arms next cycle
    assert any("auto-canceled STALE" in a for a in report.get("warnings", []))


def test_gate_refuses_sub_rr_trigger_at_arm_keeps_clean_one(tmp_path):
    # CP9 arm-time RR-floor guard (cy68): a stop_entry armed below the gate's MIN_RR=2.0 would only
    # fire then RR-veto (entry==trigger ⇒ RR fixed), so it is REFUSED at arm — never written to the
    # store. A clean >=2.0 trigger on a distinct key is armed normally. Refuse-only, never opens.
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    ex = FakeExchange({"BTC/USDT:USDT": _uptrend()})
    last = _pf(state_dir, memory_dir, ex)["briefs"][0]["last_close"]
    report = gate_execute_step(
        ex, _settings(), state_dir, memory_dir, now=NOW, cycle_no=1, proposals=[],
        regime_state=_regime("risk_off"),
        triggers=[
            # short, RR = 7/5 = 1.4 < 2.0 -> refused
            {"symbol": "BTCUSDT", "direction": "short", "kind": "stop_entry",
             "trigger_level": last - 25.0, "stop": last - 20.0,
             "take_profits": [last - 32.0], "atr": 2.0},
            # long (distinct key), RR = 15/3 = 5.0 >= 2.0 -> armed
            {"symbol": "BTCUSDT", "direction": "long", "kind": "stop_entry",
             "trigger_level": last + 25.0, "stop": last + 22.0,
             "take_profits": [last + 40.0], "atr": 2.0},
        ])
    assert report["triggers_refused_low_rr"] == 1
    assert report["triggers_armed"] == 1                 # only the clean >=2.0 trigger
    stored = load_pending_orders(state_dir)
    assert {o.direction for o in stored} == {"long"}     # the sub-RR short was NOT persisted
    assert report["opened"] == 0                         # refuse-only, nothing opened
    assert any("refused LOW-RR" in a for a in report.get("warnings", []))


def _range_flat(n=60):
    # ~100 flat (low_vol_range quadrant on the completed frame) -> the coiling-range tape where the
    # cy139-152 far breakdown stop_entries never fired.
    import numpy as np
    import pandas as pd
    closes = np.full(n, 100.0) + np.random.default_rng(7).normal(0, 0.02, n)
    return pd.DataFrame({
        "timestamp": pd.date_range("2026-01-01", periods=n, freq="4h", tz="UTC"),
        "open": closes, "high": closes + 0.2, "low": closes - 0.2, "close": closes, "volume": 1.0})


def test_gate_refuses_unreachable_breakdown_in_range_keeps_reachable(tmp_path):
    # CP10 arm-time REACHABILITY guard (cy152, 0/27 fire rate): in a RANGE quadrant a stop_entry
    # placed >1.5 ATR beyond the mark cannot print a CLOSE-through inside its expiry -> REFUSED at
    # arm (never written), while a reachable (<=1.5 ATR) trigger on a distinct key arms normally.
    # Refuse-only, never opens; symmetric long/short; the RR>=2 / wrong-side guards still apply.
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    ex = FakeExchange({"BTC/USDT:USDT": _range_flat()})
    last = _pf(state_dir, memory_dir, ex)["briefs"][0]["last_close"]
    report = gate_execute_step(
        ex, _settings(), state_dir, memory_dir, now=NOW, cycle_no=1, proposals=[],
        regime_state=_regime("risk_off"),
        triggers=[
            # SHORT breakdown 2.0 ATR below the mark -> unreachable in range -> refused
            {"symbol": "BTCUSDT", "direction": "short", "kind": "stop_entry",
             "trigger_level": last - 4.0, "stop": last - 2.0,
             "take_profits": [last - 16.0], "atr": 2.0},
            # LONG breakout 1.0 ATR above the mark (distinct key), RR 6 -> reachable -> armed
            {"symbol": "BTCUSDT", "direction": "long", "kind": "stop_entry",
             "trigger_level": last + 2.0, "stop": last,
             "take_profits": [last + 14.0], "atr": 2.0},
        ])
    assert report["triggers_refused_unreachable"] == 1
    assert report["triggers_armed"] == 1                 # only the reachable trigger
    stored = load_pending_orders(state_dir)
    assert {o.direction for o in stored} == {"long"}     # the far short was NOT persisted
    assert report["opened"] == 0                         # refuse-only, nothing opened
    assert any("UNREACHABLE" in a for a in report.get("warnings", []))


def test_gate_reachability_guard_spares_trend_quadrant(tmp_path):
    # Scope guard: the reachability veto is RANGE-only. In a TREND quadrant a far breakdown CAN run
    # to its level, so a 2-ATR-away short is NOT refused as unreachable (it arms normally). This is
    # what keeps the fix from over-vetoing genuine trend continuation entries.
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    ex = FakeExchange({"BTC/USDT:USDT": _uptrend()})     # trend quadrant, not range
    last = _pf(state_dir, memory_dir, ex)["briefs"][0]["last_close"]
    report = gate_execute_step(
        ex, _settings(), state_dir, memory_dir, now=NOW, cycle_no=1, proposals=[],
        regime_state=_regime("risk_off"),
        triggers=[
            {"symbol": "BTCUSDT", "direction": "short", "kind": "stop_entry",
             "trigger_level": last - 4.0, "stop": last - 2.0,
             "take_profits": [last - 16.0], "atr": 2.0},      # 2 ATR away but TREND -> not refused
        ])
    assert report["triggers_refused_unreachable"] == 0
    assert report["triggers_armed"] == 1                 # trend breakdown armed normally


def test_gate_refuses_stacked_add_trigger_on_open_same_direction(tmp_path):
    # CP11 arm-time no-stack guard (cy176 ETH @1752, cy178 BTC @63050): the desk has NO ADD/scale-
    # in, so a trigger whose (symbol, direction) matches an ALREADY-OPEN position is refused at arm.
    # This is the phantom add a re-anchored trigger leaves when its OLD level fired-and-opened the
    # cycle. Refuse-only, never opens; the open position itself is untouched.
    from futures_fund.state import Position, save_positions
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    ex = FakeExchange({"BTC/USDT:USDT": _uptrend()})
    last = _pf(state_dir, memory_dir, ex)["briefs"][0]["last_close"]
    save_positions(state_dir, [Position(
        symbol="BTCUSDT", direction="long", qty=0.1, entry=last, stop=last - 20.0,
        take_profits=[last + 999.0], leverage=3.0, margin=10.0, liq_price=last - 50.0,
        opened_cycle=0, opened_ts=NOW)])
    report = gate_execute_step(
        ex, _settings(), state_dir, memory_dir, now=NOW, cycle_no=1, proposals=[],
        management=[{"symbol": "BTCUSDT", "action": "hold"}],   # keep the long open
        regime_state=_regime("risk_on"),
        triggers=[
            # SAME direction as the open BTC long -> phantom ADD -> refused
            {"symbol": "BTCUSDT", "direction": "long", "kind": "stop_entry",
             "trigger_level": last + 25.0, "stop": last + 22.0,
             "take_profits": [last + 40.0], "atr": 2.0},
        ])
    assert report["triggers_refused_stacked_add"] == 1
    assert not any(o.symbol == "BTCUSDT" and o.direction == "long"
                   for o in load_pending_orders(state_dir))   # phantom add NOT persisted
    assert report["opened"] == 0                              # refuse-only
    assert "BTCUSDT" in {p.symbol for p in load_positions(state_dir)}  # open position untouched
    assert any("STACKED-ADD" in a for a in report.get("warnings", []))


def test_gate_surfaces_held_symbol_trigger_drop(tmp_path):
    # check_pending_orders CONSUMES an armed trigger whose symbol is now HELD (no stacking on a live
    # position) into none of fired/expired/remaining — previously a SILENT drop that vanished with
    # no counter and made a redundant cancel_triggers on it read triggers_canceled=0 (cy179 BTC
    # @63050 canceled while BTC long open). The drop is now counted (triggers_dropped_held).
    from futures_fund.state import Position, save_positions
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    ex = FakeExchange({"BTC/USDT:USDT": _uptrend()})
    last = _pf(state_dir, memory_dir, ex)["briefs"][0]["last_close"]
    save_positions(state_dir, [Position(
        symbol="BTCUSDT", direction="long", qty=0.1, entry=last, stop=last - 20.0,
        take_profits=[last + 999.0], leverage=3.0, margin=10.0, liq_price=last - 50.0,
        opened_cycle=0, opened_ts=NOW)])
    # a PRE-EXISTING armed BTC trigger on the now-held symbol (unfired level, won't fire or expire)
    save_pending_orders(state_dir, [PendingOrder(
        symbol="BTCUSDT", direction="long", kind="stop_entry", trigger_level=last + 100.0,
        stop=last + 90.0, take_profits=[last + 200.0], atr=2.0, created_cycle=1, expires_cycle=99)])
    report = gate_execute_step(ex, _settings(), state_dir, memory_dir, now=NOW, cycle_no=2,
                               proposals=[], management=[{"symbol": "BTCUSDT", "action": "hold"}],
                               regime_state=_regime("risk_on"))
    assert report["triggers_dropped_held"] == 1              # counted, no longer silent
    assert not any(o.symbol == "BTCUSDT" for o in load_pending_orders(state_dir))  # consumed
    assert "BTCUSDT" in {p.symbol for p in load_positions(state_dir)}  # open position untouched
    assert any("HELD-SYMBOL" in a for a in report.get("warnings", []))


def test_gate_stack_guard_spares_opposite_direction_hedge(tmp_path):
    # Scope guard: the no-stack veto is SAME-direction only. Holding a BTC long, a BTC SHORT
    # breakdown trigger (a hedge/flip) is a distinct (symbol, direction) key and is NOT refused.
    from futures_fund.state import Position, save_positions
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    ex = FakeExchange({"BTC/USDT:USDT": _uptrend()})
    last = _pf(state_dir, memory_dir, ex)["briefs"][0]["last_close"]
    save_positions(state_dir, [Position(
        symbol="BTCUSDT", direction="long", qty=0.1, entry=last, stop=last - 20.0,
        take_profits=[last + 999.0], leverage=3.0, margin=10.0, liq_price=last - 50.0,
        opened_cycle=0, opened_ts=NOW)])
    report = gate_execute_step(
        ex, _settings(), state_dir, memory_dir, now=NOW, cycle_no=1, proposals=[],
        management=[{"symbol": "BTCUSDT", "action": "hold"}],
        regime_state=_regime("risk_off"),   # with-regime short (no cr-conversion noise)
        triggers=[
            # OPPOSITE direction (short) -> a hedge, distinct key -> NOT refused as a stacked add
            {"symbol": "BTCUSDT", "direction": "short", "kind": "stop_entry",
             "trigger_level": last - 25.0, "stop": last - 22.0,
             "take_profits": [last - 40.0], "atr": 2.0},
        ])
    assert report["triggers_refused_stacked_add"] == 0
    assert any(o.symbol == "BTCUSDT" and o.direction == "short"
               for o in load_pending_orders(state_dir))     # hedge armed normally


def test_gate_extends_short_expiry_for_reachable_stop_entry_by_reach(tmp_path):
    # cy152 follow-up to CP10: a reachable but farther stop_entry needs more than the team's 2-cycle
    # (8h) window to travel to AND close through its level. The arm path FLOORS expiry by reach:
    # reach ~1.0 ATR -> 2 + ceil(1.0/0.5) = 4 cycles -> expires_cycle = created(1) + 4 = 5, raising
    # the team's stingy expires_cycle=3. Never shortens; routes through the same gate at fire.
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    ex = FakeExchange({"BTC/USDT:USDT": _range_flat()})
    last = _pf(state_dir, memory_dir, ex)["briefs"][0]["last_close"]
    report = gate_execute_step(
        ex, _settings(), state_dir, memory_dir, now=NOW, cycle_no=1, proposals=[],
        regime_state=_regime("risk_off"),
        triggers=[
            # SHORT breakdown 1.0 ATR below the mark (reachable, RR 4), team set a stingy +2 expiry
            {"symbol": "BTCUSDT", "direction": "short", "kind": "stop_entry",
             "trigger_level": last - 2.0, "stop": last - 0.5,
             "take_profits": [last - 8.0], "atr": 2.0, "expires_cycle": 3},
        ])
    assert report["triggers_armed"] == 1
    assert report["triggers_expiry_extended"] == 1
    stored = load_pending_orders(state_dir)
    assert len(stored) == 1 and stored[0].expires_cycle == 5      # created 1 + adaptive 4


def test_gate_keeps_unstamped_trigger_and_does_not_autocancel(tmp_path):
    # an UNSTAMPED prior trigger (no arm-time anchor) is never auto-canceled, even with swing_low
    # below it: a non-firing short stays armed (proves stamping is REQUIRED to retire a trigger).
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    ex = FakeExchange({"BTC/USDT:USDT": _uptrend()})
    last = _pf(state_dir, memory_dir, ex)["briefs"][0]["last_close"]
    save_pending_orders(state_dir, [_armed("short", trigger=last - 25.0, stop=last - 15.0,
                                           tps=[last - 40.0], anchor=None)])   # unstamped
    report = gate_execute_step(ex, _settings(), state_dir, memory_dir, now=NOW, cycle_no=1,
                               proposals=[], regime_state=_regime("risk_off"))
    assert report["auto_canceled_stale"] == 0 and report["triggers_remaining"] == 1


def test_gate_stamps_anchor_swing_on_new_stop_entry(tmp_path):
    # self-priming: a freshly-armed Trader stop_entry is stamped with the current directional swing
    # (short -> swing_low) so it can be revalidated in a LATER cycle; it is NOT canceled this cycle.
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    ex = FakeExchange({"BTC/USDT:USDT": _uptrend()})
    last = _pf(state_dir, memory_dir, ex)["briefs"][0]["last_close"]
    report = gate_execute_step(
        ex, _settings(), state_dir, memory_dir, now=NOW, cycle_no=1, proposals=[],
        regime_state=_regime("risk_off"),
        triggers=[{"symbol": "BTCUSDT", "direction": "short", "kind": "stop_entry",
                   "trigger_level": last - 25.0, "stop": last - 20.0,   # RR 15/5=3.0 clears floor
                   "take_profits": [last - 40.0], "atr": 2.0}])
    stored = load_pending_orders(state_dir)
    assert len(stored) == 1 and stored[0].anchor_swing is not None
    assert stored[0].anchor_swing < last - 10        # short anchored to swing_low (uptrend ~131)
    assert report["auto_canceled_stale"] == 0        # a freshly-armed trigger is never revalidated


def test_gate_stamps_anchor_swing_on_counter_regime_conversion(tmp_path):
    # PROVENANCE PARITY: a counter-regime short -> confirmation stop_entry must ALSO be stamped with
    # the arm-time swing (short -> swing_low), so a cr-safety trigger is auto-revalidatable next
    # cycle just like a Trader-emitted one. The stale GEOMETRY itself is covered by the unit tests;
    # this only proves the cr_armed provenance is no longer silently skipped.
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    ex = FakeExchange({"BTC/USDT:USDT": _uptrend()})
    last = _pf(state_dir, memory_dir, ex)["briefs"][0]["last_close"]
    report = gate_execute_step(ex, _settings(), state_dir, memory_dir, now=NOW, cycle_no=1,
                               proposals=[_short(last)], regime_state=_regime("risk_on"))
    assert report["counter_regime_triggered"] == 1 and report["auto_canceled_stale"] == 0
    stored = load_pending_orders(state_dir)
    assert len(stored) == 1 and stored[0].kind == "stop_entry"
    assert stored[0].anchor_swing is not None
    assert stored[0].anchor_swing < last - 10        # short anchored to swing_low (uptrend ~131)


# ---- Pillar 4 AUDIT: anti-hallucination — a fabricated entry/atr is dropped before the gate ----

def test_gate_audit_drops_fabricated_market_entry(tmp_path):
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    ex = FakeExchange({"BTC/USDT:USDT": _uptrend()})
    last = _pf(state_dir, memory_dir, ex)["briefs"][0]["last_close"]
    gt = {"BTCUSDT": {"mark": last, "atr": 2.0}}
    # a LONG market proposal whose entry is 50% above the brief mark = a fantasy fill -> dropped
    fab = AgentProposal(symbol="BTCUSDT", direction="long", entry=last * 1.5, stop=last * 1.4,
                        take_profits=[last * 1.8], atr=2.0, confidence=0.7,
                        rationale="x").model_dump()
    report = gate_execute_step(ex, _settings(), state_dir, memory_dir, now=NOW, cycle_no=1,
                               proposals=[fab], regime_state=_regime("risk_on"), ground_truth=gt)
    assert report["audit_dropped"] == 1 and report["opened"] == 0
    assert any("AUDIT dropped" in w for w in report.get("warnings", []))


def test_gate_audit_keeps_clean_proposal(tmp_path):
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    ex = FakeExchange({"BTC/USDT:USDT": _uptrend()})
    last = _pf(state_dir, memory_dir, ex)["briefs"][0]["last_close"]
    gt = {"BTCUSDT": {"mark": last, "atr": 2.0}}
    clean = AgentProposal(symbol="BTCUSDT", direction="long", entry=last, stop=last - 4.0,
                          take_profits=[last + 9.0], atr=2.0, confidence=0.7,
                          rationale="x").model_dump()
    report = gate_execute_step(ex, _settings(), state_dir, memory_dir, now=NOW, cycle_no=1,
                               proposals=[clean], regime_state=_regime("risk_on"), ground_truth=gt)
    assert report["audit_dropped"] == 0


def test_gate_audit_failopen_without_ground_truth(tmp_path):
    # no ground_truth passed -> audit is inert (fail-open), proposal flows to the gate as before
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    ex = FakeExchange({"BTC/USDT:USDT": _uptrend()})
    last = _pf(state_dir, memory_dir, ex)["briefs"][0]["last_close"]
    clean = AgentProposal(symbol="BTCUSDT", direction="long", entry=last, stop=last - 4.0,
                          take_profits=[last + 9.0], atr=2.0, confidence=0.7,
                          rationale="x").model_dump()
    report = gate_execute_step(ex, _settings(), state_dir, memory_dir, now=NOW, cycle_no=1,
                               proposals=[clean], regime_state=_regime("risk_on"))
    assert report["audit_dropped"] == 0


class _CryptoOnlyExchange(_UnionExchange):
    """_UnionExchange that classifies XAUUSDT (tokenized gold) as NON-crypto — exercises the
    desk's crypto-only gate. is_crypto_raw is the one method the real FuturesExchange adds."""
    def is_crypto_raw(self, raw_id):
        return raw_id != "XAUUSDT"  # XAU = COMMODITY (not a coin); everything else is crypto

    def symbol_spec(self, symbol):
        from futures_fund.models import MmrBracket, SymbolSpec
        raw = {"BTC/USDT:USDT": "BTCUSDT", "XAU/USDT:USDT": "XAUUSDT"}.get(symbol, "BTCUSDT")
        return SymbolSpec(symbol=raw, tick_size=0.01, step_size=0.001, min_notional=5.0,
                          mmr_brackets=[MmrBracket(notional_floor=0, notional_cap=1_000_000,
                                                   mmr=0.004, maint_amount=0.0, max_leverage=125)])


def test_gate_auto_retires_noncrypto_trigger_even_when_it_would_fire(tmp_path):
    # CP8: an armed trigger resting on a NON-crypto market (XAU) is retired through the normal gate
    # flow — dropped from BOTH fired (never opens) and remaining (not persisted) — while a COIN
    # trigger on the same store survives. Mirrors the stale-trigger auto-revalidation invariant.
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    ex = _CryptoOnlyExchange({"BTC/USDT:USDT": _uptrend(), "XAU/USDT:USDT": _uptrend()},
                             {"BTCUSDT": "BTC/USDT:USDT", "XAUUSDT": "XAU/USDT:USDT"})
    last = _pf(state_dir, memory_dir, ex)["briefs"][0]["last_close"]
    save_pending_orders(state_dir, [
        # XAU short WOULD fire (uptrend close < trigger 99999) — must still be retired, never opened
        PendingOrder(symbol="XAUUSDT", direction="short", kind="stop_entry", trigger_level=99999.0,
                     stop=100000.0, take_profits=[1.0], atr=2.0, created_cycle=1, expires_cycle=9),
        # BTC long does NOT fire (close < trigger) — a healthy COIN trigger that must SURVIVE
        PendingOrder(symbol="BTCUSDT", direction="long", kind="stop_entry", trigger_level=last + 50.0,
                     stop=last - 8.0, take_profits=[last + 100.0], atr=2.0, created_cycle=1,
                     expires_cycle=9),
    ])
    report = gate_execute_step(ex, _settings(), state_dir, memory_dir, now=NOW, cycle_no=2,
                               proposals=[])
    assert report["auto_retired_noncrypto"] == 1
    assert report["opened"] == 0  # the would-fire XAU short never opened a position
    store = {o.symbol for o in load_pending_orders(state_dir)}
    assert "XAUUSDT" not in store and "BTCUSDT" in store  # stock retired, coin survives
    # operator visibility (Rule 6): the retirement is surfaced
    assert any("NON-CRYPTO" in w for w in report.get("warnings", []))


def test_gate_never_arms_noncrypto_trigger(tmp_path):
    # CP7: even if a non-crypto trigger reaches the Trader output, it is never WRITTEN to the store.
    # Direction-agnostic: a LONG and a SHORT non-crypto trigger are both refused; the COIN survives.
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    ex = _CryptoOnlyExchange({"BTC/USDT:USDT": _uptrend()},
                             {"BTCUSDT": "BTC/USDT:USDT", "XAUUSDT": "XAU/USDT:USDT"})
    last = _pf(state_dir, memory_dir, ex)["briefs"][0]["last_close"]
    triggers = [
        # clean RR=50/6 long (stop just below trigger) clears the CP9 RR floor, isolates CP7
        {"symbol": "BTCUSDT", "direction": "long", "kind": "stop_entry", "trigger_level": last + 50.0,
         "stop": last + 44.0, "take_profits": [last + 100.0], "atr": 2.0, "expires_cycle": 9},
        {"symbol": "XAUUSDT", "direction": "short", "kind": "stop_entry", "trigger_level": 4264.0,
         "stop": 4288.0, "take_profits": [4205.0], "atr": 25.0, "expires_cycle": 9},
        {"symbol": "XAUUSDT", "direction": "long", "kind": "stop_entry", "trigger_level": 4400.0,
         "stop": 4300.0, "take_profits": [4600.0], "atr": 25.0, "expires_cycle": 9},
    ]
    report = gate_execute_step(ex, _settings(), state_dir, memory_dir, now=NOW, cycle_no=1,
                               proposals=[], triggers=triggers)
    store = {o.symbol for o in load_pending_orders(state_dir)}
    assert store == {"BTCUSDT"}  # both XAU sides refused at arm-time; only the coin persisted


def test_gate_purge_retires_resting_noncrypto_trigger_cp8_isolated(tmp_path):
    # CP8-ISOLATING: a non-crypto trigger that does NOT fire (price condition unmet) rests in
    # `remaining`; ONLY the CP8 purge can retire it (the open-refuse and arm-block guards never touch
    # a resting non-firing trigger). This test FAILS if CP8 is removed — it isolates that guard.
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    ex = _CryptoOnlyExchange({"BTC/USDT:USDT": _uptrend(), "XAU/USDT:USDT": _uptrend()},
                             {"BTCUSDT": "BTC/USDT:USDT", "XAUUSDT": "XAU/USDT:USDT"})
    last = _pf(state_dir, memory_dir, ex)["briefs"][0]["last_close"]
    save_pending_orders(state_dir, [
        # XAU long does NOT fire (close << trigger 99999) -> rests in `remaining`; CP8 is its only exit
        PendingOrder(symbol="XAUUSDT", direction="long", kind="stop_entry", trigger_level=99999.0,
                     stop=99000.0, take_profits=[100000.0], atr=2.0, created_cycle=1, expires_cycle=9),
        PendingOrder(symbol="BTCUSDT", direction="long", kind="stop_entry", trigger_level=last + 50.0,
                     stop=last - 8.0, take_profits=[last + 100.0], atr=2.0, created_cycle=1,
                     expires_cycle=9),
    ])
    report = gate_execute_step(ex, _settings(), state_dir, memory_dir, now=NOW, cycle_no=2,
                               proposals=[])
    assert report["triggers_fired"] == 0  # the resting case: neither trigger fires
    assert report["auto_retired_noncrypto"] == 1
    store = {o.symbol for o in load_pending_orders(state_dir)}
    assert "XAUUSDT" not in store and "BTCUSDT" in store  # resting non-crypto purged by CP8 alone


def _pinned_settings():
    from futures_fund.config import Settings
    # simulates a config.symbols pin / --symbols override that smuggles a non-crypto past the scout
    return Settings(account_size_usdt=10_000.0,
                    symbols=["BTC/USDT:USDT", "XAU/USDT:USDT"], timeframe="4h")


def test_gate_refuses_noncrypto_market_open(tmp_path):
    # CP-proposals: the deterministic gate is THE authority — a WITH-regime MARKET open on a
    # non-crypto symbol (XAU pinned via config.symbols/--symbols, or a Trader error) is REFUSED,
    # never opened. Closes the leak that the crypto gate covered scout+triggers but NOT opens.
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    settings = _pinned_settings()
    ex = _CryptoOnlyExchange({"BTC/USDT:USDT": _uptrend(), "XAU/USDT:USDT": _uptrend()},
                             {"BTCUSDT": "BTC/USDT:USDT", "XAUUSDT": "XAU/USDT:USDT"})
    pf = preflight_step(ex, settings, state_dir, memory_dir, now=NOW, cycle_no=1,
                        http_client=_HttpClient())
    xau = next(b for b in pf["briefs"] if b["exchange_id"] == "XAUUSDT")["last_close"]
    proposal = AgentProposal(symbol="XAUUSDT", direction="long", entry=xau, stop=xau - 4.0,
                             take_profits=[xau + 12.0], atr=2.0, confidence=0.7,
                             rationale="non-crypto open must be refused").model_dump()
    report = gate_execute_step(ex, settings, state_dir, memory_dir, now=NOW, cycle_no=1,
                               proposals=[proposal], regime_state=_regime("risk_on"))
    assert report["opened"] == 0  # the with-regime non-crypto open was REFUSED
    assert all(p.symbol != "XAUUSDT" for p in load_positions(state_dir))  # never held
    assert report["auto_retired_noncrypto"] >= 1
    assert any("NON-CRYPTO" in w for w in report.get("warnings", []))


def test_gate_force_closes_held_noncrypto_position(tmp_path):
    # A HELD non-crypto position (e.g. a legacy XAU long) is force-CLOSED through the normal flow —
    # the desk is crypto-only and cannot carry a tokenized stock/commodity. Direction-symmetric.
    from futures_fund.state import Position, save_positions
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    settings = _pinned_settings()
    ex = _CryptoOnlyExchange({"BTC/USDT:USDT": _uptrend(), "XAU/USDT:USDT": _uptrend()},
                             {"BTCUSDT": "BTC/USDT:USDT", "XAUUSDT": "XAU/USDT:USDT"})
    held = Position(symbol="XAUUSDT", direction="long", qty=1.0, entry=100.0, stop=90.0,
                    take_profits=[9999.0], leverage=3.0, margin=33.3, liq_price=70.0,
                    opened_cycle=0, opened_ts=NOW)  # TP/stop unreachable -> would HOLD if crypto
    save_positions(state_dir, [held])
    report = gate_execute_step(ex, settings, state_dir, memory_dir, now=NOW, cycle_no=2,
                               proposals=[], management=[])  # team gave no explicit close
    assert "XAUUSDT" not in {p.symbol for p in load_positions(state_dir)}  # force-closed by the gate
    assert report["auto_retired_noncrypto"] >= 1
    assert any("NON-CRYPTO" in w for w in report.get("warnings", []))


def test_cp9_uses_adaptive_floor(tmp_path):
    # with the quadrant floor learned to 1.6, a RR-1.7 stop_entry is ARMED (the 2.0 constant refused
    # it — see test_gate_refuses_sub_rr_trigger_at_arm_keeps_clean_one). Proves CP9 reads the floor.
    from futures_fund.rr_floor import QUADRANTS, save_rr_floor
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    ex = FakeExchange({"BTC/USDT:USDT": _uptrend()})
    last = _pf(state_dir, memory_dir, ex)["briefs"][0]["last_close"]
    save_rr_floor(state_dir, {q: 1.6 for q in QUADRANTS} | {"updated_cycle": 1})  # floors -> 1.6
    report = gate_execute_step(
        ex, _settings(), state_dir, memory_dir, now=NOW, cycle_no=2, proposals=[],
        regime_state=_regime("risk_off"),
        triggers=[{"symbol": "BTCUSDT", "direction": "short", "kind": "stop_entry",
                   "trigger_level": last - 1.0, "stop": last,             # risk 1.0
                   "take_profits": [last - 2.7], "atr": 1.0}])            # reward 1.7 -> RR 1.7
    assert report["triggers_refused_low_rr"] == 0 and report["triggers_armed"] == 1


def _rr_vetoed_shorts(n, quadrant="low_vol_range", tag="w"):
    return [{"symbol": "BTCUSDT", "direction": "short", "entry": 100.0, "stop": 101.0,
             "take_profits": [95.0], "reason": "RR 1.70 < min 2.00",
             "quadrant": quadrant, "id": f"0:{tag}:{i}"} for i in range(n)]


def test_resolve_and_adapt_loosens_on_shadow_wins(tmp_path):
    from futures_fund.orchestration import _resolve_and_adapt_rr_floor
    from futures_fund.rr_floor import load_rr_floor
    from futures_fund.shadow import record_shadow
    state_dir = tmp_path / "s"
    record_shadow(state_dir, NOW, 0, _rr_vetoed_shorts(8))
    changes = _resolve_and_adapt_rr_floor(             # short wins: forward bar low 94 <= tp 95
        state_dir, lambda sym, after: [{"high": 100.5, "low": 94.0}], cycle_no=5)
    assert load_rr_floor(state_dir)["low_vol_range"] == 1.95
    assert changes and any("low_vol_range" in c for c in changes)


def test_resolve_pending_does_not_adapt(tmp_path):
    from futures_fund.orchestration import _resolve_and_adapt_rr_floor
    from futures_fund.rr_floor import SEED, load_rr_floor
    from futures_fund.shadow import record_shadow
    state_dir = tmp_path / "s"
    record_shadow(state_dir, NOW, 0, _rr_vetoed_shorts(8, tag="p"))
    # forward bar touches neither tp 95 nor stop 101, and 1 bar < HORIZON -> pending -> no samples
    changes = _resolve_and_adapt_rr_floor(
        state_dir, lambda sym, after: [{"high": 100.5, "low": 99.0}], cycle_no=5)
    assert load_rr_floor(state_dir)["low_vol_range"] == SEED and changes == []


def test_resolve_failsafe_on_partial_ledger_entry(tmp_path):
    # a hand-corrupted RR-veto ledger entry missing `symbol` must be SKIPPED, never crash the phase
    from futures_fund.orchestration import _resolve_and_adapt_rr_floor
    from futures_fund.rr_floor import SEED, load_rr_floor
    from futures_fund.shadow import record_shadow
    state_dir = tmp_path / "s"
    record_shadow(state_dir, NOW, 0, [{"id": "0:x", "reason": "RR 1.70 < min 2.00",
                                       "quadrant": "low_vol_range", "direction": "short",
                                       "entry": 100.0, "stop": 101.0,
                                       "take_profits": [95.0]}])   # NO "symbol" key
    changes = _resolve_and_adapt_rr_floor(state_dir, lambda sym, after: [{"high": 1, "low": 1}], 5)
    assert changes == [] and load_rr_floor(state_dir)["low_vol_range"] == SEED   # skipped, no throw


def test_resolve_fresh_veto_no_forward_bars_is_pending(tmp_path):
    # a veto with no forward bars yet (bars_for returns []) stays unscored -> no adaptation
    from futures_fund.orchestration import _resolve_and_adapt_rr_floor
    from futures_fund.rr_floor import SEED, load_rr_floor
    from futures_fund.shadow import load_scored, record_shadow
    state_dir = tmp_path / "s"
    record_shadow(state_dir, NOW, 0, _rr_vetoed_shorts(8, tag="fresh"))
    changes = _resolve_and_adapt_rr_floor(state_dir, lambda sym, after: [], cycle_no=5)
    assert changes == [] and load_rr_floor(state_dir)["low_vol_range"] == SEED
    assert load_scored(state_dir) == {}            # nothing resolved (no forward bars)


def _flat_then_spike(n=60):
    # 59 flat bars (low_vol_range) + 1 spike last bar -> FORMING frame flips to high_vol_trend
    import numpy as np
    import pandas as pd
    closes = np.full(n, 100.0) + np.random.default_rng(0).normal(0, 0.02, n)
    closes[-1] = 112.0
    return pd.DataFrame({
        "timestamp": pd.date_range("2026-01-01", periods=n, freq="4h", tz="UTC"),
        "open": closes, "high": closes + 0.2, "low": closes - 0.2, "close": closes, "volume": 1.0})


def test_gate_floor_quadrant_uses_completed_frame_not_forming(tmp_path):
    # Defect-1 guard: with `now` INSIDE the last (forming) bar's window, the gate must derive the
    # RR-floor quadrant from the COMPLETED frame (matching CP9 / how triggers fire), NOT the forming
    # frame. A low-RR proposal is vetoed; its shadow entry must carry the completed quadrant.
    from futures_fund.shadow import shadow_ledger
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    ex = FakeExchange({"BTC/USDT:USDT": _flat_then_spike()})
    now_forming = dt.datetime(2026, 1, 10, 22, 0, tzinfo=UTC)   # inside the spike bar's 4h window
    preflight_step(ex, _settings(), state_dir, memory_dir, now=now_forming, cycle_no=1,
                   http_client=_HttpClient())
    prop = AgentProposal(symbol="BTCUSDT", direction="long", entry=100.0, stop=96.0,
                         take_profits=[104.0], atr=2.0, confidence=0.7,   # RR=4/4=1.0
                         rationale="x").model_dump()
    gate_execute_step(ex, _settings(), state_dir, memory_dir, now=now_forming, cycle_no=1,
                      proposals=[prop], regime_state=_regime("risk_on"))
    led = [e for e in shadow_ledger(state_dir) if str(e.get("reason", "")).startswith("RR")]
    assert led and led[0]["quadrant"] == "low_vol_range"   # COMPLETED frame, not forming


def test_fire_time_profit_lock_ladder_ratchets_a_fired_stop_entry(tmp_path):
    """#268 regression — a confirmation-gated long stop_entry that FIRES deep in profit (fills at
    the trigger BELOW the firing candle's close, which ran higher) has its stop ratcheted toward
    profit AT FIRE-TIME (deterministic, no LLM), with entry_stop recording the ORIGINAL stop. This
    is the cy97 BTC fix: a freshly-fired winner is protected through the gap to the next management
    cycle / an outage, instead of round-tripping to its original stop."""
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    f = _uptrend()
    li = f.index[-1]
    # firing candle: traded DOWN through the 146.5 trigger (low 145) then CLOSED deep in profit at
    # 149.0 (high 149.5) -> a long stop_entry fills ~146.5 (R~1.5) and the SUSTAINED close-excursion
    # is ~+1.6R, which unlocks the +1.5R rung (close-anchored, not the wick) -> stop ratchets +0.5R.
    f.loc[li, "open"], f.loc[li, "high"] = 146.5, 149.5
    f.loc[li, "low"], f.loc[li, "close"] = 145.0, 149.0
    ex = FakeExchange({"BTC/USDT:USDT": f})
    _pf(state_dir, memory_dir, ex)
    save_pending_orders(state_dir, [PendingOrder(
        symbol="BTCUSDT", direction="long", kind="stop_entry", trigger_level=146.5,
        stop=145.0, take_profits=[150.0], atr=1.5, created_cycle=0, expires_cycle=9)])
    report = gate_execute_step(ex, _settings(), state_dir, memory_dir, now=NOW, cycle_no=1,
                               proposals=[], regime_state=_regime("risk_on"))
    assert report["triggers_fired"] == 1 and report["opened"] == 1
    assert report.get("profit_locks_ratcheted", 0) >= 1
    pos = load_positions(state_dir)[0]
    assert abs(pos.entry_stop - 145.0) < 0.01    # the ORIGINAL stop recorded, not the ratcheted one
    assert pos.stop > pos.entry                  # ratcheted into PROFIT (above the ~146.5 fill)
    assert pos.stop > pos.entry_stop             # and tighter than the original stop


# --------------------------------------------------- missed-candle replay (aggregate-window exits)

def _replay_frame(now, n=60):
    """A 4h frame ending at the FORMING bar (open-ts == floor4(now)); mild uptrend so indicators are
    well-defined. The two trailing completed bars' lows are overridden by `_staged_replay` below."""
    import numpy as np
    import pandas as pd

    from futures_fund.scheduling import floor4
    rng = np.random.default_rng(7)
    close = 100.0 + 0.2 * np.arange(n) + rng.normal(0, 0.05, n)
    idx = pd.date_range(end=floor4(now), periods=n, freq="4h", tz="UTC")
    return pd.DataFrame({"timestamp": idx, "open": close, "high": close + 0.2,
                         "low": close - 0.2, "close": close, "volume": 1.0})


def _staged_replay(now):
    """Frame where the MISSED bar (16:00, index -3) dipped THROUGH the stop while the latest
    completed bar (20:00, index -2) recovered ABOVE it. Returns (frame, entry, stop)."""
    f = _replay_frame(now)
    entry = float(f["close"].iloc[-2])         # latest COMPLETED bar (20:00) close
    stop = entry - 5.0
    f.loc[f.index[-3], "low"] = entry - 8.0    # 16:00 (missed) dipped through the stop
    f.loc[f.index[-2], "low"] = entry - 2.0    # 20:00 (latest) recovered above the stop
    return f, entry, stop


def _seed_btc_long(state_dir, entry, stop):
    from futures_fund.state import Position, save_positions
    save_positions(state_dir, [Position(
        symbol="BTCUSDT", direction="long", qty=1.0, entry=entry, stop=stop,
        take_profits=[entry + 1000.0], leverage=3.0, margin=entry / 3.0, liq_price=entry - 500.0,
        opened_cycle=0, opened_ts=dt.datetime(2026, 2, 20, tzinfo=UTC))])


def _write_cycle_report(state_dir, n, candle):
    import json
    d = state_dir / "cycle" / str(n)
    d.mkdir(parents=True, exist_ok=True)
    (d / "report.json").write_text(json.dumps({"cycle": n, "candle": candle, "actions": []}))


def _replay_ex(f):
    return _UnionExchange({"BTC/USDT:USDT": f}, {"BTCUSDT": "BTC/USDT:USDT"})


def test_missed_candle_replay_honors_a_stop_touched_during_the_gap(tmp_path):
    """Regression: after an outage the gate resumes and would serve only the LATEST candle. A long
    whose stop was touched during a MISSED candle (price recovered above it by the latest bar) must
    STILL exit at the stop — the gap window honors every completed candle since the last-served one.
    Without the floor (the counterfactual tests below) the recovered position wrongly stays open."""
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    now = dt.datetime(2026, 3, 1, tzinfo=UTC)
    f, entry, stop = _staged_replay(now)
    _seed_btc_long(state_dir, entry, stop)
    # prior completed cycle served the 12:00 candle; an outage skipped 16:00 and 20:00 -> a gap.
    _write_cycle_report(state_dir, 1, "2026-02-28T12:00:00+00:00")
    ctx = preflight_step(_replay_ex(f), _settings(), state_dir, memory_dir, now=now, cycle_no=2,
                         http_client=_HttpClient())
    assert ctx["audit"]["closed"] == 1 and load_positions(state_dir) == []  # exited at gapped stop


def test_no_gap_single_bar_leaves_a_recovered_position_open(tmp_path):
    """No-gap identity: when the prior served candle is the immediately-preceding one (normal +4h
    cadence) the window is the single latest bar — the recovered low is above the stop, so the
    position stays open exactly as today. Proves the change only ADDS missed exits, never invents.
    """
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    now = dt.datetime(2026, 3, 1, tzinfo=UTC)
    f, entry, stop = _staged_replay(now)
    _seed_btc_long(state_dir, entry, stop)
    _write_cycle_report(state_dir, 1, "2026-02-28T20:00:00+00:00")  # prior served the latest candle
    ctx = preflight_step(_replay_ex(f), _settings(), state_dir, memory_dir, now=now, cycle_no=2,
                         http_client=_HttpClient())
    assert ctx["audit"]["closed"] == 0 and len(load_positions(state_dir)) == 1


def test_cold_start_no_prior_cycle_uses_single_latest_bar(tmp_path):
    """No prior completed cycle -> last_served_candle is None -> single latest bar (today's
    behavior); the recovered low is above the stop, so the position is held, not closed."""
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    now = dt.datetime(2026, 3, 1, tzinfo=UTC)
    f, entry, stop = _staged_replay(now)
    _seed_btc_long(state_dir, entry, stop)
    ctx = preflight_step(_replay_ex(f), _settings(), state_dir, memory_dir, now=now, cycle_no=1,
                         http_client=_HttpClient())
    assert ctx["audit"]["closed"] == 0 and len(load_positions(state_dir)) == 1


def _staged_gap_fill(now):
    """The MISSED bar (16:00, index -3) GAPS OPEN below the stop while the EARLIEST window bar
    (12:00, index -4) opens ABOVE it — so the honest fill is the gapped LATER open, not the stop
    level (the optimism the earlier first_open produced). Returns (frame, entry, stop)."""
    f = _replay_frame(now)
    entry = float(f["close"].iloc[-2])              # latest COMPLETED bar (20:00) close
    stop = entry - 5.0
    f.loc[f.index[-3], "open"] = entry - 20.0       # 16:00 GAPS DOWN, opens far below the stop
    f.loc[f.index[-3], "high"] = entry - 19.8
    f.loc[f.index[-3], "low"] = entry - 22.0
    f.loc[f.index[-2], "low"] = entry - 2.0         # 20:00 recovered above the stop
    return f, entry, stop


def test_missed_candle_replay_fills_a_gapped_stop_at_the_honest_open(tmp_path):
    """Conservatism (adversarial-review fix): when a MISSED bar GAPS OPEN below a long's stop, the
    exit fills at that gapped open — the WORSE of (stop, open) — not the unreachable stop level. The
    gapped bar (16:00) is LATER than the earliest missed bar (12:00), so this pins the directional
    min-open fix: the realized loss reflects the ~$20/unit gap, not the ~$5/unit stop."""
    from futures_fund.state import load_account
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    now = dt.datetime(2026, 3, 1, tzinfo=UTC)
    f, entry, stop = _staged_gap_fill(now)
    _seed_btc_long(state_dir, entry, stop)
    _write_cycle_report(state_dir, 1, "2026-02-28T12:00:00+00:00")  # outage skipped 16:00 + 20:00
    before = load_account(state_dir, 10_000.0).balance
    ctx = preflight_step(_replay_ex(f), _settings(), state_dir, memory_dir, now=now, cycle_no=2,
                         http_client=_HttpClient())
    after = load_account(state_dir, 10_000.0).balance
    assert ctx["audit"]["closed"] == 1
    loss = before - after
    assert loss > 15.0   # decisively the gapped open (~$20), NOT the stop level (~$5, the old bug)

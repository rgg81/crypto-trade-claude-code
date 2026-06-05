"""#2 trigger orders: hybrid fill (stop-entry on CLOSE break, limit-entry on TOUCH), knife guard,
wrong-side reject, no-bar unevaluable, fire-before-expiry, held-skip, corrupt-store fail-safe."""
import json

from futures_fund.pending_orders import (
    PendingOrder,
    check_pending_orders,
    fired_to_proposal,
    load_pending_orders,
    save_pending_orders,
    upsert_triggers,
)


def _o(symbol="BTCUSDT", direction="short", kind="stop_entry", trigger=100.0, stop=105.0,
       expires=99, **kw):
    return PendingOrder(symbol=symbol, direction=direction, kind=kind, trigger_level=trigger,
                        stop=stop, take_profits=kw.get("tps", [trigger * 0.9]), atr=1.0,
                        risk_mult=kw.get("risk_mult", 1.0),
                        require_oi_rising=kw.get("require_oi_rising", False),
                        created_cycle=1, expires_cycle=expires)


def _save(tmp, orders):
    save_pending_orders(tmp, orders)


def test_stop_entry_short_fires_on_close_below_trigger(tmp_path):
    _save(tmp_path, [_o(kind="stop_entry", direction="short", trigger=100, stop=105)])
    fired, expired, remaining = check_pending_orders(tmp_path, {"BTCUSDT": {"close": 99, "low": 98, "high": 101}}, 5)
    assert len(fired) == 1 and not remaining
    assert fired_to_proposal(fired[0])["entry"] == 100  # fills at trigger, not the 99 close


def test_stop_entry_short_no_fire_on_close_at_or_above(tmp_path):
    _save(tmp_path, [_o(kind="stop_entry", direction="short", trigger=100, stop=105)])
    fired, expired, remaining = check_pending_orders(tmp_path, {"BTCUSDT": {"close": 100, "low": 99, "high": 101}}, 5)
    assert not fired and len(remaining) == 1  # strict <


def test_limit_entry_long_fires_on_low_touch(tmp_path):
    _save(tmp_path, [_o(kind="limit_entry", direction="long", trigger=100, stop=95)])
    fired, _, _ = check_pending_orders(tmp_path, {"BTCUSDT": {"close": 105, "low": 99, "high": 106}}, 5)
    assert len(fired) == 1 and fired_to_proposal(fired[0])["entry"] == 100


def test_limit_entry_knife_guard_no_fire_when_bar_pierced_stop(tmp_path):
    _save(tmp_path, [_o(kind="limit_entry", direction="long", trigger=100, stop=95)])
    fired, expired, remaining = check_pending_orders(tmp_path, {"BTCUSDT": {"close": 96, "low": 94, "high": 101}}, 5)
    assert not fired and not remaining  # knife: low 94 hit trigger AND stop -> consumed, not re-armed


def test_wrong_side_stop_rejected(tmp_path):
    _save(tmp_path, [_o(kind="limit_entry", direction="long", trigger=100, stop=105)])  # stop above entry
    fired, expired, remaining = check_pending_orders(tmp_path, {"BTCUSDT": {"close": 99, "low": 98, "high": 101}}, 5)
    assert not fired and not remaining  # inverted geometry -> dropped


def test_no_bar_stays_pending_unevaluable(tmp_path):
    _save(tmp_path, [_o(symbol="ZZZUSDT", expires=99)])
    fired, expired, remaining = check_pending_orders(tmp_path, {}, 5)  # no bar for ZZZ
    assert not fired and not expired and len(remaining) == 1


def test_expiry_inclusive_and_fire_precedes_expiry(tmp_path):
    a = _o(symbol="AUSDT", kind="stop_entry", direction="short", trigger=100, stop=105, expires=5)
    b = _o(symbol="BUSDT", kind="stop_entry", direction="short", trigger=100, stop=105, expires=5)
    _save(tmp_path, [a, b])
    bars = {"AUSDT": {"close": 101, "low": 100, "high": 102},  # no fire -> expires
            "BUSDT": {"close": 99, "low": 98, "high": 101}}     # fires AND at expiry -> fires
    fired, expired, remaining = check_pending_orders(tmp_path, bars, 5)
    assert {o.symbol for o in fired} == {"BUSDT"}
    assert {o.symbol for o in expired} == {"AUSDT"}
    assert not remaining


def test_held_symbol_trigger_skipped_and_removed(tmp_path):
    _save(tmp_path, [_o(symbol="BTCUSDT")])
    fired, expired, remaining = check_pending_orders(
        tmp_path, {"BTCUSDT": {"close": 99, "low": 98, "high": 101}}, 5, held_symbols={"BTCUSDT"})
    assert not fired and not remaining and not expired  # held -> consumed/removed


def test_corrupt_store_returns_empty(tmp_path):
    (tmp_path / "pending_orders.json").write_text('[{"symbol":"BTCUSDT","direction":"short"  garbage')
    assert load_pending_orders(tmp_path) == []
    assert check_pending_orders(tmp_path, {}, 5) == ([], [], [])


def test_missing_store_cold_start_empty(tmp_path):
    assert load_pending_orders(tmp_path) == []
    assert check_pending_orders(tmp_path, {"BTCUSDT": {"close": 1}}, 5) == ([], [], [])


def test_upsert_replaces_by_symbol_dir_kind(tmp_path):
    existing = [_o(symbol="BTCUSDT", direction="short", kind="stop_entry", trigger=100)]
    new = [_o(symbol="BTCUSDT", direction="short", kind="stop_entry", trigger=90)]  # same key, new level
    merged = upsert_triggers(existing, new)
    assert len(merged) == 1 and merged[0].trigger_level == 90


def test_fired_trigger_carries_risk_mult():
    # a PendingOrder's risk_mult must survive into the fired AgentProposal dict (default 1.0)
    from futures_fund.pending_orders import PendingOrder, fired_to_proposal
    o = PendingOrder(symbol="ENAUSDT", direction="short", kind="stop_entry",
                     trigger_level=0.09, stop=0.0995, take_profits=[0.0691], atr=0.0095,
                     risk_mult=0.5)
    assert fired_to_proposal(o)["risk_mult"] == 0.5
    o2 = PendingOrder(symbol="BTCUSDT", direction="long", kind="stop_entry",
                      trigger_level=100.0, stop=95.0, take_profits=[110.0], atr=2.0)
    assert fired_to_proposal(o2)["risk_mult"] == 1.0


def test_counter_regime_trigger_preserves_risk_mult():
    # a counter-regime proposal carrying a half-size risk_mult must survive the rewrite to a
    # confirmation stop_entry (else it would silently fire at full size when confirmed).
    from futures_fund.orchestration import _proposal_to_stop_entry
    po = _proposal_to_stop_entry(
        {"symbol": "ENAUSDT", "direction": "long", "entry": 0.10, "stop": 0.095,
         "take_profits": [0.12], "atr": 0.005, "risk_mult": 0.5}, cycle_no=5)
    assert po.risk_mult == 0.5
    # default preserved when absent
    po2 = _proposal_to_stop_entry(
        {"symbol": "BTCUSDT", "direction": "short", "entry": 100.0, "stop": 105.0,
         "take_profits": [90.0], "atr": 2.0}, cycle_no=5)
    assert po2.risk_mult == 1.0


# --- OI-confirmation gate (require_oi_rising) -------------------------------------------------
# A stop_entry that OPTS IN (require_oi_rising=True) may fire on its price-close break ONLY IF OI is
# rising (fresh fuel) at fire time; a spent-OI break is a bounce-trap and HOLDS the trigger armed.
# Default False = today's behavior (OI never consulted). Symmetric: identical for long and short.

def test_oi_gate_short_fires_when_oi_rising(tmp_path):
    _save(tmp_path, [_o(direction="short", trigger=100, stop=105, require_oi_rising=True)])
    fired, _, remaining = check_pending_orders(
        tmp_path, {"BTCUSDT": {"close": 99, "low": 98, "high": 101}}, 5,
        oi_change_by_symbol={"BTCUSDT": 0.10})
    assert len(fired) == 1 and not remaining


def test_oi_gate_short_holds_when_oi_bleeding(tmp_path):
    _save(tmp_path, [_o(direction="short", trigger=100, stop=105, require_oi_rising=True)])
    fired, _, remaining = check_pending_orders(
        tmp_path, {"BTCUSDT": {"close": 99, "low": 98, "high": 101}}, 5,
        oi_change_by_symbol={"BTCUSDT": -0.09})
    assert not fired and len(remaining) == 1  # break printed but OI spent -> stays armed


def test_oi_gate_long_fires_when_oi_rising(tmp_path):
    _save(tmp_path, [_o(direction="long", trigger=100, stop=95, require_oi_rising=True)])
    fired, _, remaining = check_pending_orders(
        tmp_path, {"BTCUSDT": {"close": 101, "low": 99, "high": 102}}, 5,
        oi_change_by_symbol={"BTCUSDT": 0.10})
    assert len(fired) == 1 and not remaining  # mirror of the short -> locks long/short symmetry


def test_oi_gate_long_holds_when_oi_bleeding(tmp_path):
    _save(tmp_path, [_o(direction="long", trigger=100, stop=95, require_oi_rising=True)])
    fired, _, remaining = check_pending_orders(
        tmp_path, {"BTCUSDT": {"close": 101, "low": 99, "high": 102}}, 5,
        oi_change_by_symbol={"BTCUSDT": -0.09})
    assert not fired and len(remaining) == 1


def test_oi_gate_default_no_op_ignores_oi(tmp_path):
    # require_oi_rising defaults False -> OI never consulted; fires even with bleeding/absent OI
    _save(tmp_path, [_o(direction="short", trigger=100, stop=105)])
    fired, _, _ = check_pending_orders(
        tmp_path, {"BTCUSDT": {"close": 99, "low": 98, "high": 101}}, 5,
        oi_change_by_symbol={"BTCUSDT": -0.50})
    assert len(fired) == 1
    _save(tmp_path, [_o(direction="short", trigger=100, stop=105)])
    fired2, _, _ = check_pending_orders(  # no oi arg at all (the ~40 existing call sites)
        tmp_path, {"BTCUSDT": {"close": 99, "low": 98, "high": 101}}, 5)
    assert len(fired2) == 1


def test_oi_gate_failsafe_holds_on_missing_or_nan_oi(tmp_path):
    # require_oi_rising + missing/None/NaN OI -> fail-closed: hold armed, never a spurious fire,
    # applied IDENTICALLY to long and short (a feed outage cannot create one-sided bias).
    for oi_arg in (None, {}, {"BTCUSDT": None}, {"BTCUSDT": float("nan")}):
        for direction, close, stop in (("short", 99, 105), ("long", 101, 95)):
            _save(tmp_path, [_o(direction=direction, trigger=100, stop=stop,
                                require_oi_rising=True)])
            fired, _, remaining = check_pending_orders(
                tmp_path, {"BTCUSDT": {"close": close, "low": 98, "high": 102}}, 5,
                oi_change_by_symbol=oi_arg)
            assert not fired and len(remaining) == 1, (oi_arg, direction)


def test_oi_gate_symmetry_feed_outage_holds_both_long_and_short(tmp_path):
    # market-neutral invariant (HARD RULE 5): on ONE feed outage for a symbol, an opted-in LONG and
    # SHORT on that SAME symbol must BOTH hold — no asymmetric suppression. long_trigger < close <
    # short_trigger so both price-breaks are satisfied; the OI gate (None -> hold) suppresses both.
    _save(tmp_path, [
        _o(direction="long", kind="stop_entry", trigger=98, stop=92, require_oi_rising=True),
        _o(direction="short", kind="stop_entry", trigger=102, stop=108, require_oi_rising=True),
    ])
    fired, _, remaining = check_pending_orders(
        tmp_path, {"BTCUSDT": {"close": 100, "low": 99, "high": 101}}, 5,
        oi_change_by_symbol={"BTCUSDT": None})
    assert not fired and len(remaining) == 2


def test_oi_gate_flat_oi_does_not_fire(tmp_path):
    # flat OI (0.0) is below the +0.5% rising deadband -> not 'rising' -> hold
    _save(tmp_path, [_o(direction="short", trigger=100, stop=105, require_oi_rising=True)])
    fired, _, remaining = check_pending_orders(
        tmp_path, {"BTCUSDT": {"close": 99, "low": 98, "high": 101}}, 5,
        oi_change_by_symbol={"BTCUSDT": 0.0})
    assert not fired and len(remaining) == 1


def test_oi_gate_field_persists_roundtrip(tmp_path):
    save_pending_orders(tmp_path, [_o(direction="short", trigger=100, stop=105,
                                      require_oi_rising=True)])
    reloaded = load_pending_orders(tmp_path)
    assert len(reloaded) == 1 and reloaded[0].require_oi_rising is True
    # a LEGACY record without the field validates with require_oi_rising == False (back-compat)
    legacy = {"symbol": "ETHUSDT", "direction": "short", "kind": "stop_entry",
              "trigger_level": 100.0, "stop": 105.0, "take_profits": [90.0], "atr": 1.0,
              "created_cycle": 1, "expires_cycle": 99}
    (tmp_path / "pending_orders.json").write_text(json.dumps([legacy]))
    assert load_pending_orders(tmp_path)[0].require_oi_rising is False


def test_counter_regime_trigger_preserves_require_oi_rising():
    # a Trader-opted-in require_oi_rising must survive the counter-regime rewrite; absent -> False
    # (a counter-regime SAFETY trigger is NOT double-gated on OI -> no spurious suppression on a
    # feed outage).
    from futures_fund.orchestration import _proposal_to_stop_entry
    po = _proposal_to_stop_entry(
        {"symbol": "ETHUSDT", "direction": "short", "entry": 1622.0, "stop": 1700.0,
         "take_profits": [1500.0], "atr": 50.0, "require_oi_rising": True}, cycle_no=5)
    assert po.require_oi_rising is True
    po2 = _proposal_to_stop_entry(
        {"symbol": "BTCUSDT", "direction": "long", "entry": 64800.0, "stop": 60800.0,
         "take_profits": [70000.0], "atr": 1652.0}, cycle_no=5)
    assert po2.require_oi_rising is False

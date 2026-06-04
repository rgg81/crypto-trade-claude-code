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

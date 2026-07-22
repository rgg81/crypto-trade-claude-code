"""Regression for the verify-pass HIGH bug: a stand-down/HALT proposals.json that OMITS or nulls
the 'management' key must NOT fall through to close-by-absence (which flattens the whole book).
The agent path always carries a holdings review (possibly empty) — coerced to [] here so
gate_execute_step sees has_review=True and keeps holdings."""
from futures_fund.orchestration import (
    management_review,
    unmatched_management_symbols,
    unsupported_management_actions,
)


def _ident(s):
    return s


def test_unmatched_management_all_match():
    mgmt = [{"symbol": "SOLUSDT", "action": "hold"}]
    assert unmatched_management_symbols(mgmt, {"SOLUSDT"}, _ident) == []


def test_unmatched_management_flags_wrong_symbol():
    # cy90 cross-reference class: a management entry for a symbol that is NOT an open position
    # is a SILENT no-op (the intended close/trail never happens) — must be surfaced LOUD.
    mgmt = [{"symbol": "SUIUSDT", "action": "close"}]
    assert unmatched_management_symbols(mgmt, {"SOLUSDT"}, _ident) == ["SUIUSDT"]


def test_unmatched_management_resolver_used():
    # the resolver maps a unified symbol to its raw form before matching
    mgmt = [{"symbol": "SOL/USDT:USDT", "action": "hold"}]
    resolver = {"SOL/USDT:USDT": "SOLUSDT"}.get
    assert unmatched_management_symbols(mgmt, {"SOLUSDT"}, lambda s: resolver(s, s)) == []


def test_unmatched_management_empty_and_garbage_safe():
    assert unmatched_management_symbols([], {"SOLUSDT"}, _ident) == []
    assert unmatched_management_symbols(None, {"SOLUSDT"}, _ident) == []
    assert unmatched_management_symbols(["not-a-dict"], {"SOLUSDT"}, _ident) == []


def test_unsupported_action_all_recognized_ok():
    # close / reduce / hold are the only actions the gate's management loop dispatches on —
    # none of them should be flagged.
    mgmt = [
        {"symbol": "BTCUSDT", "action": "hold", "new_stop": 65820},
        {"symbol": "ETHUSDT", "action": "reduce", "reduce_fraction": 0.5},
        {"symbol": "SOLUSDT", "action": "close"},
        {"symbol": "AAVEUSDT", "action": "hold"},  # bare hold legitimately no-ops
    ]
    assert unsupported_management_actions(mgmt) == []


def test_unsupported_action_flags_extend_tp():
    # cy275 gap: an `extend_tp` (or any action the gate cannot execute) on a HELD symbol matches
    # no dispatch branch and falls through to held-unchanged — a SILENT no-op that falsely implies
    # the TP was widened. Must be surfaced LOUD (twin of the cy90 wrong-symbol class).
    mgmt = [{"symbol": "BTCUSDT", "action": "extend_tp", "new_tp": 69000}]
    assert unsupported_management_actions(mgmt) == [{"symbol": "BTCUSDT", "action": "extend_tp"}]


def test_unsupported_action_flags_missing_action():
    # a management entry with no action at all is also a silent no-op — flag it.
    assert unsupported_management_actions([{"symbol": "BTCUSDT"}]) == [
        {"symbol": "BTCUSDT", "action": None}]


def test_unsupported_action_empty_and_garbage_safe():
    assert unsupported_management_actions([]) == []
    assert unsupported_management_actions(None) == []
    assert unsupported_management_actions(["not-a-dict"]) == []


def test_missing_key_is_empty_list_not_none():
    assert management_review({"proposals": []}) == []


def test_explicit_null_is_empty_list_not_none():
    assert management_review({"proposals": [], "management": None}) == []


def test_present_empty_list_preserved():
    assert management_review({"proposals": [], "management": []}) == []


def test_populated_review_preserved():
    review = [{"symbol": "BNBUSDT", "action": "hold", "new_stop": None}]
    assert management_review({"management": review}) == review


def test_reduce_directive_preserved():
    review = [{"symbol": "ETHUSDT", "action": "reduce", "reduce_fraction": 0.5, "reason": "bank half"}]
    assert management_review({"management": review}) == review


def test_never_returns_none():
    # whatever the payload shape, the agent path must never yield None (would -> close_absent=True)
    for payload in ({}, {"management": None}, {"proposals": []}, {"management": []}):
        assert management_review(payload) is not None

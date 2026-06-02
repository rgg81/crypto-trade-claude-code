"""Regression for the verify-pass HIGH bug: a stand-down/HALT proposals.json that OMITS or nulls
the 'management' key must NOT fall through to close-by-absence (which flattens the whole book).
The agent path always carries a holdings review (possibly empty) — coerced to [] here so
gate_execute_step sees has_review=True and keeps holdings."""
from futures_fund.orchestration import management_review


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

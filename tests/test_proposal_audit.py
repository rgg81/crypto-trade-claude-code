"""Anti-hallucination proposal audit (Pillar 4): cross-check agent entry/atr vs brief ground truth;
drop clear fabrications; fail-open on missing data; symmetric long/short."""
from futures_fund.proposal_audit import (
    audit_atr,
    audit_batch,
    audit_entry,
    audit_item,
)


def test_market_entry_near_mark_ok():
    ok, _ = audit_entry(101.0, 100.0, is_trigger=False)   # 1% off -> ok
    assert ok


def test_market_entry_far_from_mark_dropped():
    ok, reason = audit_entry(130.0, 100.0, is_trigger=False)  # 30% off a MARKET open -> fabrication
    assert not ok and "deviates" in reason


def test_trigger_entry_gets_wider_band():
    # 20% below mark is fine for a breakdown TRIGGER but would fail a market open
    assert audit_entry(80.0, 100.0, is_trigger=True)[0] is True
    assert audit_entry(80.0, 100.0, is_trigger=False)[0] is False


def test_trigger_entry_too_far_dropped():
    ok, _ = audit_entry(60.0, 100.0, is_trigger=True)  # 40% > 25% trigger tol
    assert not ok


def test_atr_in_family_ok_out_of_family_dropped():
    assert audit_atr(2.0, 2.5)[0] is True            # 0.8x -> ok
    assert audit_atr(10.0, 2.0)[0] is False          # 5x -> fabrication
    assert audit_atr(0.1, 2.0)[0] is False           # 0.05x -> fabrication


def test_fail_open_on_missing_ground_truth():
    assert audit_entry(130.0, None, is_trigger=False)[0] is True   # no mark -> can't check -> keep
    assert audit_atr(99.0, None)[0] is True                        # no brief atr -> keep
    assert audit_atr(99.0, 0.0)[0] is True                         # non-positive -> keep
    assert audit_item({"symbol": "X", "entry": 999, "atr": 999}, {}, is_trigger=False)[0] is True


def test_symmetry_long_short_identical():
    # the audit is direction-agnostic: same entry/mark/atr -> same verdict regardless of side
    gt = {"BTCUSDT": {"mark": 100.0, "atr": 2.0}}
    longp = {"symbol": "BTCUSDT", "direction": "long", "entry": 130.0, "atr": 2.0}
    shortp = {"symbol": "BTCUSDT", "direction": "short", "entry": 130.0, "atr": 2.0}
    assert audit_item(longp, gt, is_trigger=False)[0] == audit_item(shortp, gt, is_trigger=False)[0]
    assert audit_item(longp, gt, is_trigger=False)[0] is False


def test_trigger_audits_on_trigger_level_not_a_divergent_entry():
    # A trigger fires/rests at `trigger_level`; the anti-hallucination audit must check THAT level,
    # not a divergent `entry` key. (Exposed by the resting-order reroute: a misrouted dict can carry
    # both keys.) A hallucinated trigger_level 91% off mark must DROP even if `entry` looks clean.
    gt = {"X": {"mark": 100.0, "atr": 2.0}}
    item = {"symbol": "X", "trigger_level": 9.0, "entry": 100.0, "atr": 2.0}
    assert audit_item(item, gt, is_trigger=True)[0] is False        # audited on trigger_level (9)
    # symmetric mirror: a MARKET open audits on `entry`, ignoring a clean-looking trigger_level
    mkt = {"symbol": "X", "entry": 9.0, "trigger_level": 100.0, "atr": 2.0}
    assert audit_item(mkt, gt, is_trigger=False)[0] is False        # audited on entry (9)


def test_single_keyed_orders_audit_unchanged():
    # back-compat: a trigger with only trigger_level, a market open with only entry -> unchanged
    gt = {"X": {"mark": 100.0, "atr": 2.0}}
    assert audit_item({"symbol": "X", "trigger_level": 101.0, "atr": 2.0}, gt, is_trigger=True)[0]
    assert audit_item({"symbol": "X", "entry": 101.0, "atr": 2.0}, gt, is_trigger=False)[0]
    # a trigger missing trigger_level falls back to entry; a market open missing entry -> trig_level
    assert audit_item({"symbol": "X", "entry": 101.0, "atr": 2.0}, gt, is_trigger=True)[0]
    assert audit_item({"symbol": "X", "trigger_level": 101.0, "atr": 2.0}, gt, is_trigger=False)[0]


def test_audit_batch_partitions_and_tags_reason():
    gt = {"AAA": {"mark": 100.0, "atr": 2.0}, "BBB": {"mark": 50.0, "atr": 1.0}}
    items = [
        {"symbol": "AAA", "entry": 100.5, "atr": 2.0},   # clean
        {"symbol": "BBB", "entry": 80.0, "atr": 1.0},    # 60% off market -> drop
        {"symbol": "CCC", "entry": 999.0, "atr": 999.0},  # no ground truth -> keep (fail-open)
    ]
    kept, dropped = audit_batch(items, gt, is_trigger=False)
    assert {k["symbol"] for k in kept} == {"AAA", "CCC"}
    assert [d["symbol"] for d in dropped] == ["BBB"]
    assert "_audit_reason" in dropped[0]

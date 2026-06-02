"""FLAT-decision journal: persist declined setups + later evaluate whether standing aside cost us.
This is the data source that lets the Reflector mint ENABLING ('DO take it') lessons, not only
'don't' lessons (the one-directional ratchet)."""
from datetime import UTC, datetime

from futures_fund.flat_journal import (
    append_flat_decision,
    evaluate_pending_flats,
    read_flat_decisions,
)


def _flat(symbol, *, edge=True, side="long", mark=100.0, cycle=9):
    return {"cycle": cycle, "symbol": symbol, "regime": "high_vol_trend", "rating": "flat",
            "reason": "no fillable entry", "edge_aligned": edge, "favored_side": side, "mark": mark}


def test_append_and_read_roundtrip(tmp_path):
    fid = append_flat_decision(tmp_path, _flat("XLMUSDT"), ts=datetime(2026, 5, 31, tzinfo=UTC))
    rows = read_flat_decisions(tmp_path)
    assert len(rows) == 1 and rows[0]["id"] == fid
    assert rows[0]["edge_aligned"] is True and rows[0]["evaluated"] is False


def test_evaluate_marks_flat_that_cost_us(tmp_path):
    # passed on a LONG at 100; price later 105 (+5% our way) -> standing aside COST us
    append_flat_decision(tmp_path, _flat("XLMUSDT", side="long", mark=100.0),
                         ts=datetime(2026, 5, 31, tzinfo=UTC))
    n = evaluate_pending_flats(tmp_path, {"XLMUSDT": 105.0}, datetime(2026, 5, 31, 4, tzinfo=UTC))
    assert n == 1
    r = read_flat_decisions(tmp_path)[0]
    assert r["evaluated"] is True and r["flat_cost_us"] is True
    assert abs(r["favored_move_pct"] - 0.05) < 1e-9


def test_evaluate_marks_flat_that_was_right(tmp_path):
    # passed on a LONG at 100; price later 96 (-4% against the long) -> standing aside was RIGHT
    append_flat_decision(tmp_path, _flat("ZECUSDT", side="long", mark=100.0),
                         ts=datetime(2026, 5, 31, tzinfo=UTC))
    evaluate_pending_flats(tmp_path, {"ZECUSDT": 96.0}, datetime(2026, 5, 31, 4, tzinfo=UTC))
    r = read_flat_decisions(tmp_path)[0]
    assert r["flat_cost_us"] is False


def test_short_side_favored_move_sign(tmp_path):
    # passed on a SHORT at 100; price later 90 (-10%) -> favored move is +10% (cost us)
    append_flat_decision(tmp_path, _flat("SUIUSDT", side="short", mark=100.0),
                         ts=datetime(2026, 5, 31, tzinfo=UTC))
    evaluate_pending_flats(tmp_path, {"SUIUSDT": 90.0}, datetime(2026, 5, 31, 4, tzinfo=UTC))
    r = read_flat_decisions(tmp_path)[0]
    assert abs(r["favored_move_pct"] - 0.10) < 1e-9 and r["flat_cost_us"] is True


def test_non_edge_aligned_flats_are_not_evaluated(tmp_path):
    append_flat_decision(tmp_path, _flat("DOGEUSDT", edge=False), ts=datetime(2026, 5, 31, tzinfo=UTC))
    n = evaluate_pending_flats(tmp_path, {"DOGEUSDT": 200.0}, datetime(2026, 5, 31, 4, tzinfo=UTC))
    assert n == 0 and read_flat_decisions(tmp_path)[0]["evaluated"] is False


def test_evaluate_is_idempotent(tmp_path):
    append_flat_decision(tmp_path, _flat("XLMUSDT", mark=100.0), ts=datetime(2026, 5, 31, tzinfo=UTC))
    evaluate_pending_flats(tmp_path, {"XLMUSDT": 105.0}, datetime(2026, 5, 31, 4, tzinfo=UTC))
    again = evaluate_pending_flats(tmp_path, {"XLMUSDT": 110.0}, datetime(2026, 5, 31, 8, tzinfo=UTC))
    assert again == 0  # already evaluated, not re-scored

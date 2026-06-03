"""#3 r_progress fix: the holding card's R-progress must measure gain vs the ORIGINAL risk
(journaled, never-trailed stop), so a profit-locked stop (trailed past entry) no longer flips the
denominator into the garbage +4.25 we observed. Tests the matrix rows directly on _holding_card."""
from datetime import UTC, datetime, timedelta

from futures_fund.orchestration import _holding_card
from futures_fund.state import Position


def _pos(direction, entry, stop, **kw):
    return Position(symbol="XUSDT", direction=direction, qty=10.0, entry=entry, stop=stop,
                    take_profits=kw.get("tps", [entry * 1.3]), leverage=4.0, margin=100.0,
                    liq_price=kw.get("liq", entry * 0.5 if direction == "long" else entry * 1.5),
                    opened_cycle=1, opened_ts=datetime(2026, 6, 1, tzinfo=UTC),
                    decision_id=kw.get("decision_id", "d1"))


def _card(pos, mark, decision):
    return _holding_card(pos, {"mark_price": mark}, datetime(2026, 6, 1, 4, tzinfo=UTC), "4h", decision)


def test_r_progress_profit_locked_long_uses_original_stop():
    # journal original stop 90 (risk 10); stop trailed to 110 (profit-locked); mark 147
    pos = _pos("long", 100.0, 110.0)
    r = _card(pos, 147.0, {"entry": 100.0, "stop": 90.0})["r_progress"]
    assert r == 4.7  # (147-100)/10 ; NOT garbage from /(100-110)


def test_r_progress_profit_locked_short_uses_original_stop():
    # short original stop 110 (risk 10); trailed to 95; mark 80 -> +2.0R
    pos = _pos("short", 100.0, 95.0)
    r = _card(pos, 80.0, {"entry": 100.0, "stop": 110.0})["r_progress"]
    assert r == 2.0  # -1*(80-100)/abs(100-110) ; trailed-stop denom would wrongly give +4.0


def test_r_progress_legacy_no_decision_falls_back():
    pos = _pos("long", 100.0, 95.0, decision_id=None)
    r = _card(pos, 110.0, None)["r_progress"]
    assert r == 2.0  # falls back to abs(100-95)=5 ; (110-100)/5


def test_r_progress_entry_equals_original_stop_finite():
    import math
    pos = _pos("long", 100.0, 100.0)
    r = _card(pos, 150.0, {"entry": 100.0, "stop": 100.0})["r_progress"]
    assert math.isfinite(r)  # zero-div guard (or 1e-9)


def test_r_progress_slipped_entry_denominator_uses_pos_entry():
    # journal entry 100.0 but filled pos.entry 100.02; original stop 90 -> denom abs(100.02-90)
    pos = _pos("long", 100.02, 110.0)
    r = _card(pos, 150.0, {"entry": 100.0, "stop": 90.0})["r_progress"]
    assert abs(r - round((150.0 - 100.02) / (100.02 - 90.0), 2)) < 1e-9


def test_r_progress_invariant_to_trail_count():
    # journal original stop fixed at 90, mark fixed; r_progress identical regardless of trailed stop
    dec = {"entry": 100.0, "stop": 90.0}
    rs = [_card(_pos("long", 100.0, st), 130.0, dec)["r_progress"] for st in (90.0, 95.0, 110.0, 125.0)]
    assert len(set(rs)) == 1 and rs[0] == 3.0  # (130-100)/10, never moves with the trail


def test_r_progress_decision_stop_missing_field_falls_back():
    pos = _pos("long", 100.0, 95.0)
    r = _card(pos, 110.0, {"entry": 100.0, "stop": None})["r_progress"]  # stop None -> legacy
    assert r == 2.0  # abs(100-95)=5 ; (110-100)/5


def test_holding_card_prefers_completed_bar_close_over_live_mark():
    # r_progress/mark must anchor to last_close (the COMPLETED 4h bar the desk decides on), NOT the
    # live Binance mark_price — matching how triggers/exits evaluate on the 4h close.
    pos = _pos("long", 100.0, 90.0)
    card = _holding_card(pos, {"last_close": 120.0, "mark_price": 125.0},
                         datetime(2026, 6, 1, 4, tzinfo=UTC), "4h", {"entry": 100.0, "stop": 90.0})
    assert card["mark"] == 120.0                 # completed bar, not the 125.0 live mark
    assert card["r_progress"] == 2.0             # (120-100)/(100-90)=2.0, not (125-100)/10=2.5


def test_holding_card_at_risk_flag():
    # surfaces whether the leg carries downside risk (drives the risk-bearing tilt / neutralize-trail)
    dec = {"entry": 100.0, "stop": 90.0}
    locked = _holding_card(_pos("long", 100.0, 105.0), {"last_close": 120.0},
                           datetime(2026, 6, 1, 4, tzinfo=UTC), "4h", dec)
    assert locked["at_risk"] is False            # stop 105 >= entry 100 -> profit-locked
    at_risk = _holding_card(_pos("long", 100.0, 90.0), {"last_close": 120.0},
                            datetime(2026, 6, 1, 4, tzinfo=UTC), "4h", dec)
    assert at_risk["at_risk"] is True            # stop 90 < entry 100 -> loss-side, at risk
    short_locked = _holding_card(_pos("short", 100.0, 95.0), {"last_close": 80.0},
                                 datetime(2026, 6, 1, 4, tzinfo=UTC), "4h", dec)
    assert short_locked["at_risk"] is False       # short stop 95 <= entry 100 -> profit-locked

"""#1 regime arbiter: deterministic core + persistence/hysteresis + symmetric override. Fail-closed
— degraded/gappy/corrupt inputs can WITHHOLD a confident label (so the orchestrator requires
confirmation BOTH ways) but never manufacture one. `confirmed` is advisory conviction strength, not
a short gate — shorts are never blocked (market-neutral desk)."""
import json
from datetime import UTC, datetime, timedelta

from futures_fund.regime import (
    append_regime_history,
    classify_regime,
    read_regime_history,
)

NOW = datetime(2026, 6, 1, 8, 7, tzinfo=UTC)   # floor4 -> 08:00; prior candle 04:00
C0 = "2026-06-01T08:00:00+00:00"
C_PREV = "2026-06-01T04:00:00+00:00"


def _brief(sym, down=True):
    return {"exchange_id": sym, "trend_direction": "down" if down else "up",
            "momentum_20": -0.05 if down else 0.05}


def _all_majors(down=True):
    return [_brief(m, down) for m in ("BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT")]


def _mc(fng=29):
    return {"fear_greed": {"value": fng}} if fng is not None else {}


def _seed(state_dir, candle, det_regime="risk_off", cycle_no=1):
    p = state_dir / "regime_history.jsonl"
    p.write_text(json.dumps({"cycle_no": cycle_no, "candle": candle,
                             "deterministic_regime": det_regime, "regime": det_regime}) + "\n")


def test_regime_cold_start_no_history_unconfirmed(tmp_path):
    rs = classify_regime(tmp_path, _mc(28), _all_majors(True), NOW, cycle_no=10)
    assert rs.regime == "risk_off" and rs.confirmed is False  # 1 candle -> advisory-unconfirmed


def test_regime_confirms_after_K_distinct_candles(tmp_path):
    _seed(tmp_path, C_PREV, "risk_off")
    rs = classify_regime(tmp_path, _mc(28), _all_majors(True), NOW, cycle_no=11)
    assert rs.drivers["persistence_count"] >= 2
    assert rs.confirmed is True  # advisory STRENGTH of a sustained risk_off (not a short gate)


def test_regime_one_green_candle_breaks_riskoff_confirmation(tmp_path):
    _seed(tmp_path, C_PREV, "risk_off")
    rs = classify_regime(tmp_path, _mc(55), _all_majors(False), NOW, cycle_no=11)  # green flip
    assert rs.regime != "risk_off" and rs.confirmed is False


def test_regime_risk_on_confirms_symmetrically(tmp_path):
    # FIX #3: a sustained RISK_ON read earns the same durable-conviction `confirmed` stamp that a
    # sustained risk_off does (two-sided), so longs get the conviction bonus shorts get — symmetric.
    _seed(tmp_path, C_PREV, "risk_on")
    rs = classify_regime(tmp_path, _mc(80), _all_majors(False), NOW, cycle_no=11)  # all-up, greed
    assert rs.regime == "risk_on" and rs.drivers["persistence_count"] >= 2
    assert rs.confirmed is True


def test_regime_risk_on_one_candle_not_yet_confirmed(tmp_path):
    rs = classify_regime(tmp_path, _mc(80), _all_majors(False), NOW, cycle_no=11)  # cold start
    assert rs.regime == "risk_on" and rs.confirmed is False  # 1 candle < K


def test_regime_retry_same_cycle_no_idempotent_append(tmp_path):
    rs = classify_regime(tmp_path, _mc(28), _all_majors(True), NOW, cycle_no=11)
    append_regime_history(tmp_path, rs)
    append_regime_history(tmp_path, rs)  # RETRY same cycle_no
    recs = [r for r in read_regime_history(tmp_path) if r["cycle_no"] == 11]
    assert len(recs) == 1


def test_regime_below_quorum_cannot_confirm(tmp_path):
    _seed(tmp_path, C_PREV, "risk_off")
    briefs = [_brief("BTCUSDT", True), _brief("ETHUSDT", True)]  # only 2 majors
    rs = classify_regime(tmp_path, _mc(28), briefs, NOW, cycle_no=11)
    assert rs.confirmed is False  # below quorum -> no confident label either way
    assert len(rs.drivers["majors_present"]) == 2


def test_regime_btc_absent_caps_confidence(tmp_path):
    _seed(tmp_path, C_PREV, "risk_off")
    briefs = [_brief("ETHUSDT", True), _brief("SOLUSDT", True), _brief("XRPUSDT", True)]  # no BTC
    rs = classify_regime(tmp_path, _mc(28), briefs, NOW, cycle_no=11)
    assert rs.confirmed is False  # BTC anchor absent -> quorum fails -> no confident label
    assert "btc_absent" in rs.drivers["degraded"]


def test_regime_degraded_feed_cannot_manufacture_risk_off(tmp_path):
    # mixed majors (no breadth signal), no F&G, no news flag -> cannot be risk_off-confirmed
    briefs = [_brief("BTCUSDT", False), _brief("ETHUSDT", True), _brief("BNBUSDT", False),
              _brief("SOLUSDT", True), _brief("XRPUSDT", False)]
    rs = classify_regime(tmp_path, {}, briefs, NOW, cycle_no=11, news_risk_off=None)
    assert rs.confirmed is False
    assert "fear_greed_missing" in rs.drivers["degraded"]
    assert "news_flag_missing" in rs.drivers["degraded"]


def test_regime_corrupt_history_line_skipped(tmp_path):
    p = tmp_path / "regime_history.jsonl"
    future = "2026-06-02T00:00:00+00:00"
    p.write_text("{ half-written\n"
                 + json.dumps({"cycle_no": 9, "candle": future, "deterministic_regime": "risk_off"}) + "\n")
    rs = classify_regime(tmp_path, _mc(28), _all_majors(True), NOW, cycle_no=11)
    assert rs.confirmed is False  # corrupt + future-dated skipped -> can't confirm


def test_regime_override_extra_confirm_keys_ignored_not_confirming(tmp_path):
    # deterministic risk_on; an agent override carrying confirmed/shorts keys can NOT force a
    # confirmed risk_off — those keys are simply ignored; confirmed stays det-driven (advisory).
    ov = {"regime": "risk_off", "confirmed": True, "shorts_permitted": True, "justification": "catalyst"}
    rs = classify_regime(tmp_path, _mc(60), _all_majors(False), NOW, cycle_no=11, agent_override=ov)
    assert rs.regime == "risk_off"          # the regime label IS forced (a long-veto / conviction bias)
    assert rs.confirmed is False            # but confirmed is det-driven; the keys did nothing
    assert rs.drivers["override"]["applied"] is True


def test_regime_override_forces_riskoff_does_not_confirm(tmp_path):
    ov = {"regime": "risk_off", "justification": "not-yet-priced exploit"}
    rs = classify_regime(tmp_path, _mc(60), _all_majors(False), NOW, cycle_no=11, agent_override=ov)
    assert rs.regime == "risk_off" and rs.confirmed is False  # biases the read, never confirms


def test_regime_override_symmetric_force_risk_on(tmp_path):
    # the SYMMETRIC mirror: an agent may force risk_on too (which makes new SHORTS counter-regime).
    ov = {"regime": "risk_on", "justification": "fresh broad risk-on catalyst"}
    rs = classify_regime(tmp_path, _mc(60), _all_majors(False), NOW, cycle_no=11, agent_override=ov)
    assert rs.regime == "risk_on" and rs.drivers["override"]["applied"] is True


def test_regime_override_derisk_always_honored(tmp_path):
    _seed(tmp_path, C_PREV, "risk_off")  # deterministic would confirm risk_off
    ov = {"regime": "risk_on", "justification": "fresh risk-on catalyst"}
    rs = classify_regime(tmp_path, _mc(28), _all_majors(True), NOW, cycle_no=11, agent_override=ov)
    assert rs.regime == "risk_on" and rs.confirmed is False  # de-risk honored; never confirmed
    assert rs.drivers["deterministic_regime"] == "risk_off"  # core read still recorded

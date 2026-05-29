from datetime import UTC, datetime

from futures_fund.shadow import record_shadow, shadow_ledger, shadow_outcome


def test_record_and_read_shadow(tmp_path):
    record_shadow(tmp_path, datetime(2026, 5, 1, tzinfo=UTC), cycle=1, entries=[
        {"symbol": "BTCUSDT", "direction": "long", "entry": 100.0, "stop": 95.0,
         "take_profits": [115.0], "reason": "RR 1.2 < min 2"}])
    led = shadow_ledger(tmp_path)
    assert len(led) == 1 and led[0]["symbol"] == "BTCUSDT" and led[0]["cycle"] == 1


def test_shadow_outcome_long_would_have_stopped_out():
    entry = {"symbol": "BTCUSDT", "direction": "long", "entry": 100.0, "stop": 95.0,
             "take_profits": [115.0]}
    # bar low pierces the stop -> the vetoed long would have lost; veto SAVED us
    out = shadow_outcome(entry, bar_high=101.0, bar_low=94.0)
    assert out["hit"] == "stop" and out["r_multiple"] < 0 and out["veto_saved"] is True


def test_shadow_outcome_long_would_have_won():
    entry = {"symbol": "BTCUSDT", "direction": "long", "entry": 100.0, "stop": 95.0,
             "take_profits": [115.0]}
    out = shadow_outcome(entry, bar_high=116.0, bar_low=99.0)
    assert out["hit"] == "take_profit" and out["r_multiple"] > 0 and out["veto_saved"] is False


def test_shadow_outcome_no_trigger():
    entry = {"symbol": "BTCUSDT", "direction": "long", "entry": 100.0, "stop": 95.0,
             "take_profits": [115.0]}
    assert shadow_outcome(entry, bar_high=108.0, bar_low=98.0)["hit"] is None


def test_shadow_outcome_short_would_have_stopped_out():
    entry = {"symbol": "BTCUSDT", "direction": "short", "entry": 100.0, "stop": 105.0,
             "take_profits": [85.0]}
    out = shadow_outcome(entry, bar_high=106.0, bar_low=99.0)
    assert out["hit"] == "stop" and out["r_multiple"] < 0 and out["veto_saved"] is True


def test_shadow_outcome_short_would_have_won():
    entry = {"symbol": "BTCUSDT", "direction": "short", "entry": 100.0, "stop": 105.0,
             "take_profits": [85.0]}
    out = shadow_outcome(entry, bar_high=101.0, bar_low=84.0)
    assert out["hit"] == "take_profit" and out["r_multiple"] > 0 and out["veto_saved"] is False

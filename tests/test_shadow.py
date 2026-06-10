from datetime import UTC, datetime

from futures_fund.shadow import (
    HORIZON,
    load_scored,
    record_shadow,
    save_scored,
    score_shadow_first_touch,
    shadow_ledger,
    shadow_outcome,
    tally_resolutions,
)


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


def _short(entry=100.0, stop=104.0, tp=88.0):   # short: tp below, stop above; risk 4, reward 12
    return {"direction": "short", "entry": entry, "stop": stop, "take_profits": [tp]}


def test_first_touch_won_short():
    bars = [{"high": 101, "low": 99}, {"high": 100, "low": 87}]   # 2nd bar low 87 <= tp 88
    assert score_shadow_first_touch(_short(), bars) == "won"


def test_first_touch_lost_short():
    assert score_shadow_first_touch(_short(), [{"high": 105, "low": 100}]) == "lost"  # 105>=stop104


def test_same_bar_tp_and_stop_resolves_lost():
    assert score_shadow_first_touch(_short(), [{"high": 105, "low": 87}]) == "lost"  # conservative


def test_pending_then_expired():
    assert score_shadow_first_touch(_short(), [{"high": 101, "low": 95}]) == "pending"
    assert score_shadow_first_touch(_short(), [{"high": 101, "low": 95}] * HORIZON) == "expired"


def test_long_mirror():
    lg = {"direction": "long", "entry": 100.0, "stop": 96.0, "take_profits": [112.0]}
    assert score_shadow_first_touch(lg, [{"high": 112, "low": 99}]) == "won"
    assert score_shadow_first_touch(lg, [{"high": 101, "low": 95}]) == "lost"


def test_tally_resolutions_excludes_undecided():
    scored = {
        "a": {"outcome": "won", "quadrant": "low_vol_range"},
        "b": {"outcome": "lost", "quadrant": "low_vol_range"},
        "c": {"outcome": "expired", "quadrant": "low_vol_range"},   # excluded
        "d": {"outcome": "won", "quadrant": "high_vol_trend"},
        "e": {"outcome": "pending", "quadrant": "low_vol_range"},   # excluded
    }
    t = tally_resolutions(scored, trail_w=40)
    assert t["low_vol_range"] == (1, 1) and t["high_vol_trend"] == (1, 0)


def test_tally_trailing_window_keeps_recent():
    scored = {str(i): {"outcome": "lost", "quadrant": "q"} for i in range(50)}
    scored["x"] = {"outcome": "won", "quadrant": "q"}              # most recent
    won, lost = tally_resolutions(scored, trail_w=5)["q"]
    assert won == 1 and lost == 4                                  # only the last 5 decided


def test_scored_cache_roundtrip(tmp_path):
    save_scored(tmp_path, {"x": {"outcome": "won", "quadrant": "low_vol_range", "cycle": 3}})
    assert load_scored(tmp_path)["x"]["outcome"] == "won"
    assert load_scored(tmp_path / "nope") == {}     # missing -> empty

import json

from futures_fund.rr_floor import (
    BAND,
    QUADRANTS,
    SEED,
    adapt_rr_floor,
    effective_rr_floor,
    load_rr_floor,
    save_rr_floor,
)


def test_seed_and_band_constants():
    assert SEED == 2.0 and BAND == (1.6, 2.5)
    assert set(QUADRANTS) == {"high_vol_trend", "low_vol_trend", "high_vol_range", "low_vol_range"}


def test_load_missing_returns_all_seed(tmp_path):
    state = load_rr_floor(tmp_path)
    assert all(state[q] == SEED for q in QUADRANTS) and state["updated_cycle"] == 0


def test_load_corrupt_or_partial_fails_safe(tmp_path):
    (tmp_path / "rr_floor.json").write_text("{ not json")
    assert all(load_rr_floor(tmp_path)[q] == SEED for q in QUADRANTS)
    (tmp_path / "rr_floor.json").write_text(json.dumps({"low_vol_range": 1.7}))
    s = load_rr_floor(tmp_path)
    assert s["low_vol_range"] == 1.7 and s["high_vol_trend"] == SEED   # missing keys -> SEED


def test_effective_clamps_to_band():
    assert effective_rr_floor("low_vol_range", {"low_vol_range": 1.4}) == 1.6   # below band
    assert effective_rr_floor("high_vol_trend", {"high_vol_trend": 3.0}) == 2.5  # above band
    assert effective_rr_floor("low_vol_trend", {"low_vol_trend": 1.9}) == 1.9    # in band
    assert effective_rr_floor("missing_q", {}) == SEED                          # unknown -> SEED


def test_save_then_load_roundtrip(tmp_path):
    save_rr_floor(tmp_path, {"high_vol_trend": 2.1, "low_vol_trend": 2.0,
                             "high_vol_range": 1.8, "low_vol_range": 1.7, "updated_cycle": 5})
    s = load_rr_floor(tmp_path)
    assert s["low_vol_range"] == 1.7 and s["updated_cycle"] == 5


def _seed_state():
    return {q: SEED for q in QUADRANTS} | {"updated_cycle": 0}


def test_adapt_loosens_when_vetoes_cost_winners():
    new, changes = adapt_rr_floor(_seed_state(), {"low_vol_range": (7, 1)}, cycle_no=10)  # w=0.875
    assert new["low_vol_range"] == 1.95 and new["updated_cycle"] == 10
    assert any("low_vol_range" in c for c in changes)


def test_adapt_tightens_when_vetoes_save_losers():
    new, _ = adapt_rr_floor(_seed_state(), {"high_vol_trend": (2, 8)}, cycle_no=11)  # w=0.2
    assert new["high_vol_trend"] == 2.10


def test_adapt_deadband_no_change():
    new, changes = adapt_rr_floor(_seed_state(), {"low_vol_trend": (5, 5)}, cycle_no=12)  # w=0.5
    assert new["low_vol_trend"] == SEED and changes == []


def test_adapt_requires_min_samples():
    new, changes = adapt_rr_floor(_seed_state(), {"low_vol_range": (7, 0)}, cycle_no=13)  # 7<8
    assert new["low_vol_range"] == SEED and changes == []


def test_adapt_clamps_at_bounds():
    st = _seed_state() | {"low_vol_range": 1.6}
    new, _ = adapt_rr_floor(st, {"low_vol_range": (8, 0)}, cycle_no=14)   # 1.55 -> clamp 1.6
    assert new["low_vol_range"] == 1.6
    st2 = _seed_state() | {"high_vol_trend": 2.5}
    new2, _ = adapt_rr_floor(st2, {"high_vol_trend": (0, 8)}, cycle_no=15)  # 2.6 -> clamp 2.5
    assert new2["high_vol_trend"] == 2.5

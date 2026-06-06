"""Regime-routed playbook (Pillar 2 ADAPT): each quadrant maps to its in-season strategies so the
desk switches playbook with the tape (trend->trend-follow, range->mean-reversion, madness->small/RV,
transition->confirm)."""
from futures_fund.playbook import _PLAYBOOK, is_range, playbook_for


def test_trend_quadrants_route_to_trend_follow():
    for q in ("low_vol_trend", "high_vol_trend"):
        pb = playbook_for(q)
        assert pb["quadrant"] == q
        assert any("trend" in s for s in pb["strategies"])
        assert not is_range(q)


def test_range_quadrants_route_to_mean_reversion():
    for q in ("low_vol_range", "high_vol_range"):
        pb = playbook_for(q)
        assert any("mean-reversion" in s or "fade" in s for s in pb["strategies"])
        assert is_range(q)


def test_madness_prefers_relative_value_and_smaller_size():
    pb = playbook_for("high_vol_range")
    assert any("relative-value" in s for s in pb["strategies"])
    assert any("reduce-size" in s or "small" in s for s in pb["strategies"])


def test_transition_is_confirmation_only():
    pb = playbook_for("transition")
    assert "confirmation-only" in pb["strategies"]


def test_unknown_quadrant_defaults_to_confirmation_only():
    pb = playbook_for("not_a_regime")
    assert pb["strategies"] == ["confirmation-only"]
    assert is_range("not_a_regime") is False


def test_every_known_quadrant_has_guidance():
    for _q, (strategies, guidance) in _PLAYBOOK.items():
        assert strategies and isinstance(guidance, str) and guidance

import pytest

from futures_fund.models import PortfolioHealth, RegimeState
from futures_fund.policy import caps_for, circuit_breaker, cvar


def _health(equity, peak):
    return PortfolioHealth(equity=equity, peak_equity=peak)


def test_healthy_low_vol_trend_is_full_caps():
    caps = caps_for(RegimeState(quadrant="low_vol_trend"), _health(10_000, 10_000))
    assert caps.max_leverage == 5.0
    assert caps.per_trade_risk_pct == pytest.approx(0.015)
    assert caps.max_heat == pytest.approx(0.10)
    assert caps.bias == "normal"


def test_high_vol_range_is_reduced():
    caps = caps_for(RegimeState(quadrant="high_vol_range"), _health(10_000, 10_000))
    assert caps.max_leverage == 2.0
    assert caps.per_trade_risk_pct == pytest.approx(0.005)


def test_caution_halves_caps():
    # equity 9400/10000 -> dd 6% -> caution tier; halve the healthy Q1 caps
    caps = caps_for(RegimeState(quadrant="low_vol_trend"), _health(9_400, 10_000))
    assert caps.max_leverage == pytest.approx(2.5)
    assert caps.per_trade_risk_pct == pytest.approx(0.0075)


def test_stressed_forces_flat_bias_and_zero_risk():
    caps = caps_for(RegimeState(quadrant="low_vol_trend"), _health(8_500, 10_000))  # dd 15%
    assert caps.bias == "flat"
    assert caps.per_trade_risk_pct == 0.0


def test_transition_regime_minimum_size():
    caps = caps_for(RegimeState(quadrant="transition"), _health(10_000, 10_000))
    assert caps.bias == "reduce"
    assert caps.max_leverage <= 2.0


def test_circuit_breaker_daily_loss_halts_new():
    state = circuit_breaker(daily_pnl_pct=-0.035, weekly_pnl_pct=-0.01, monthly_pnl_pct=-0.02,
                            dd_from_peak=0.04)
    assert state.allow_new_entries is False
    assert state.risk_multiplier <= 1.0


def test_circuit_breaker_step_down_halves_at_5pct_drawdown():
    state = circuit_breaker(daily_pnl_pct=-0.01, weekly_pnl_pct=-0.02, monthly_pnl_pct=-0.03,
                            dd_from_peak=0.06)
    assert state.risk_multiplier == pytest.approx(0.5)


def test_circuit_breaker_monthly_force_flatten():
    state = circuit_breaker(daily_pnl_pct=-0.02, weekly_pnl_pct=-0.05, monthly_pnl_pct=-0.16,
                            dd_from_peak=0.16)
    assert state.force_flatten is True


def test_cvar_is_mean_of_worst_tail():
    # returns; 5% tail of 20 obs = worst 1 obs = -0.10
    returns = [-0.10] + [0.01] * 19
    assert cvar(returns, alpha=0.05) == pytest.approx(-0.10)

import pytest

from futures_fund.metrics import (
    agent_attribution,
    calmar,
    hit_rate,
    max_drawdown,
    profit_factor,
    sharpe,
    sortino,
)


def test_sharpe_zero_for_constant_returns():
    assert sharpe([0.01, 0.01, 0.01]) == 0.0
    assert sharpe([]) == 0.0


def test_sharpe_positive_for_positive_mean():
    assert sharpe([0.0, 0.02, 0.01, 0.015]) > 0


def test_sortino_only_penalizes_downside():
    # no negative returns -> sortino is large/inf-guarded but > sharpe-ish; just assert positive
    assert sortino([0.01, 0.02, 0.0]) > 0
    assert sortino([0.01, 0.01]) >= 0


def test_max_drawdown_peak_to_trough():
    assert max_drawdown([100, 110, 90, 95, 120]) == pytest.approx((110 - 90) / 110)


def test_max_drawdown_monotonic_up_is_zero():
    assert max_drawdown([100, 101, 102]) == 0.0


def test_calmar_is_return_over_drawdown():
    assert calmar(annual_return=0.40, mdd=0.10) == pytest.approx(4.0)
    assert calmar(0.40, 0.0) == 0.0  # guard


def test_hit_rate_and_profit_factor():
    closed = [{"realized_pnl": 5.0}, {"realized_pnl": -3.0}, {"realized_pnl": 2.0}]
    assert hit_rate(closed) == pytest.approx(2 / 3)
    assert profit_factor(closed) == pytest.approx(7.0 / 3.0)


def test_profit_factor_no_losses_returns_inf_guard():
    assert profit_factor([{"realized_pnl": 5.0}]) == float("inf")
    assert hit_rate([]) == 0.0


def test_agent_attribution_sums_pnl_and_hit_rate_per_agent():
    closed = [
        {"realized_pnl": 10.0, "contributing_agents": ["research_manager", "trader"]},
        {"realized_pnl": -4.0, "contributing_agents": ["research_manager", "trader"]},
        {"realized_pnl": 6.0, "contributing_agents": ["baseline"]},
    ]
    attr = agent_attribution(closed)
    assert attr["research_manager"]["pnl"] == pytest.approx(6.0)
    assert attr["research_manager"]["count"] == 2
    assert attr["research_manager"]["hit_rate"] == pytest.approx(0.5)
    assert attr["baseline"]["pnl"] == pytest.approx(6.0)

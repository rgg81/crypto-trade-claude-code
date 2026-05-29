import pytest

from futures_fund.models import PortfolioHealth
from futures_fund.portfolio import open_heat, portfolio_health, total_equity, unrealized_pnl
from tests.test_state import _pos  # reuse the Position factory


def test_unrealized_long_and_short():
    long = _pos("BTCUSDT", "long")        # qty 0.5, entry 100
    short = _pos("ETHUSDT", "short")      # qty 0.5, entry 100
    assert unrealized_pnl(long, mark=110.0) == pytest.approx(5.0)    # 0.5*(110-100)
    assert unrealized_pnl(short, mark=110.0) == pytest.approx(-5.0)  # 0.5*(100-110)


def test_total_equity_adds_unrealized():
    positions = [_pos("BTCUSDT", "long")]   # +5 at mark 110
    eq = total_equity(balance=10_000.0, positions=positions, prices={"BTCUSDT": 110.0})
    assert eq == pytest.approx(10_005.0)


def test_total_equity_skips_missing_price():
    positions = [_pos("BTCUSDT", "long")]
    eq = total_equity(balance=10_000.0, positions=positions, prices={})  # no price -> 0 unrealized
    assert eq == pytest.approx(10_000.0)


def test_open_heat_uses_position_risk():
    # qty 0.5, stop gap 5 -> risk 2.5 on equity 10k = 0.00025
    positions = [_pos("BTCUSDT", "long")]
    assert open_heat(positions, equity=10_000.0) == pytest.approx(0.00025)


def test_portfolio_health_tracks_peak_and_drawdown():
    positions = [_pos("BTCUSDT", "long")]   # mark below entry -> loss
    h = portfolio_health(balance=10_000.0, peak_equity=10_050.0, positions=positions,
                         prices={"BTCUSDT": 90.0}, recent_hit_rate=0.6)
    assert isinstance(h, PortfolioHealth)
    # equity = 10000 + 0.5*(90-100) = 9995; peak stays 10050
    assert h.equity == pytest.approx(9995.0)
    assert h.peak_equity == 10_050.0
    assert h.recent_hit_rate == 0.6
    assert h.drawdown_from_peak == pytest.approx((10_050 - 9_995) / 10_050)


def test_portfolio_health_raises_peak_when_equity_higher():
    positions = [_pos("BTCUSDT", "long")]
    h = portfolio_health(balance=10_000.0, peak_equity=9_000.0, positions=positions,
                         prices={"BTCUSDT": 120.0}, recent_hit_rate=0.5)
    assert h.peak_equity == h.equity  # new high-water mark

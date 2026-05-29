import pytest

from futures_fund.models import (
    MmrBracket,
    PortfolioHealth,
    RegimeState,
    SymbolSpec,
    TradeProposal,
)
from futures_fund.risk_gate import GateInputs, evaluate


def _spec():
    return SymbolSpec(
        symbol="BTCUSDT", tick_size=0.1, step_size=0.001, min_notional=5.0,
        mmr_brackets=[
            MmrBracket(
                notional_floor=0, notional_cap=50_000, mmr=0.004,
                maint_amount=0.0, max_leverage=125,
            ),
        ],
    )


def _proposal(direction="long", entry=100.0, stop=95.0, tps=(115.0,)):
    return TradeProposal(symbol="BTCUSDT", direction=direction, entry=entry, stop=stop,
                         take_profits=list(tps), atr=2.0, confidence=0.7, horizon_hours=8,
                         funding_rate=0.0001)


def _inputs(**over):
    base = dict(
        proposal=_proposal(),
        spec=_spec(),
        regime=RegimeState(quadrant="low_vol_trend"),
        health=PortfolioHealth(equity=10_000.0, peak_equity=10_000.0),
        open_positions=[],
        corr={},
        daily_pnl_pct=0.0, weekly_pnl_pct=0.0, monthly_pnl_pct=0.0,
    )
    base.update(over)
    return GateInputs(**base)


def test_clean_trade_is_approved_and_leverage_is_output():
    d = evaluate(_inputs())
    assert d.verdict == "approve"
    assert d.sized_trade.leverage > 0
    # risk ~= 1.5% of equity in low_vol_trend healthy
    risk = d.sized_trade.qty * abs(100.0 - 95.0) / 10_000.0
    assert risk == pytest.approx(0.015, abs=2e-3)


def test_stressed_portfolio_vetoes_new_entry():
    d = evaluate(_inputs(health=PortfolioHealth(equity=8_500.0, peak_equity=10_000.0)))
    assert d.verdict == "veto"
    assert "flat" in d.reason.lower() or "stressed" in d.reason.lower()


def test_bad_rr_is_vetoed():
    # take-profit barely above entry -> RR < 2:1
    d = evaluate(_inputs(proposal=_proposal(tps=(101.0,))))
    assert d.verdict == "veto"
    assert "rr" in d.reason.lower() or "reward" in d.reason.lower()


def test_heat_cap_resizes_when_existing_exposure_high():
    # Pre-existing 9% heat, cap 10% -> new 1.5% trade must be resized down to fit.
    existing = [dict(symbol="ETHUSDT", direction="long", qty=180.0, entry=100.0, stop=95.0)]  # 9%
    d = evaluate(_inputs(open_positions=existing))
    assert d.verdict in ("resize", "veto")
    if d.verdict == "resize":
        new_risk = d.sized_trade.qty * 5.0 / 10_000.0
        assert new_risk <= 0.01 + 1e-6  # only ~1% of headroom remained


def test_daily_breaker_halts_new_entries():
    d = evaluate(_inputs(daily_pnl_pct=-0.04))
    assert d.verdict == "veto"


def test_cost_estimate_is_attached():
    d = evaluate(_inputs())
    assert d.sized_trade.cost.total > 0

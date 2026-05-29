import pytest

from futures_fund.consolidation import consolidate, cvar_risk_multiplier
from futures_fund.models import CostEstimate, SizedTrade, TradeProposal


def _sized(symbol, qty, entry=100.0, stop=95.0, direction="long"):
    prop = TradeProposal(symbol=symbol, direction=direction, entry=entry, stop=stop,
                         take_profits=[entry * 1.2], atr=2.0, confidence=0.6,
                         horizon_hours=4, funding_rate=0.0)
    return SizedTrade(proposal=prop, qty=qty, notional=entry * qty, leverage=5.0,
                      margin=entry * qty / 5.0, liq_price=82.0, cost=CostEstimate())


def test_cvar_multiplier_derisks_on_bad_tail():
    calm = cvar_risk_multiplier([0.01, 0.0, -0.01, 0.005], threshold=-0.05, floor=0.5)
    bad = cvar_risk_multiplier([-0.10, -0.08, 0.01, 0.0], threshold=-0.05, floor=0.5)
    assert calm == 1.0
    assert bad == 0.5


def test_cvar_multiplier_no_history_is_one():
    assert cvar_risk_multiplier([], threshold=-0.05) == 1.0


def test_consolidate_scales_book_to_gross_heat_cap():
    # two trades each risking 1% (qty 20, gap 5 on 10k); cap 0.015 -> must scale to 0.75x
    trades = [_sized("BTCUSDT", 20.0), _sized("ETHUSDT", 20.0)]
    out = consolidate(trades, equity=10_000.0, max_heat=0.015)
    total_risk = sum(t.qty * 5.0 / 10_000.0 for t in out)
    assert total_risk == pytest.approx(0.015, abs=1e-9)
    # qty scaled down proportionally
    assert out[0].qty == pytest.approx(20.0 * 0.75)


def test_consolidate_under_cap_is_unchanged():
    trades = [_sized("BTCUSDT", 10.0)]  # 0.5% risk, cap 10%
    out = consolidate(trades, equity=10_000.0, max_heat=0.10)
    assert out[0].qty == 10.0


def test_consolidate_applies_cvar_multiplier():
    trades = [_sized("BTCUSDT", 10.0)]
    out = consolidate(trades, equity=10_000.0, max_heat=0.10, cvar_mult=0.5)
    assert out[0].qty == pytest.approx(5.0)


def test_consolidate_drops_dust():
    trades = [_sized("BTCUSDT", 0.001)]  # negligible risk
    out = consolidate(trades, equity=10_000.0, max_heat=0.10, min_risk_frac=0.001)
    assert out == []

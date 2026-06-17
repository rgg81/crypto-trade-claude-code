import pytest

from futures_fund.models import (
    MmrBracket,
    PortfolioHealth,
    RegimeState,
    SymbolSpec,
    TradeProposal,
)
from futures_fund.risk_gate import HARD_MIN_RR, MIN_RR, GateInputs, evaluate


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


def test_exactly_2r_is_not_vetoed_by_float_error():
    # NEAR cycle-2 case: reward/risk is mathematically 2.0 but floats to 1.9999999999999993.
    # The float tolerance must let it through instead of a spurious "RR < 2.0" veto.
    p = _proposal(direction="short", entry=2.403, stop=2.692, tps=(1.825,))
    import futures_fund.risk_gate as rg
    assert rg._reward_risk(p) < 2.0  # genuinely floats under
    d = evaluate(_inputs(proposal=p, spec=_spec().model_copy(update={"min_notional": 1.0})))
    assert d.verdict != "veto" or "rr" not in d.reason.lower()  # NOT an RR veto


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


def test_short_trade_is_approved_with_liq_above_entry():
    # short: stop above entry, take-profit below entry (RR 3:1)
    prop = _proposal(direction="short", entry=100.0, stop=105.0, tps=(85.0,))
    d = evaluate(_inputs(proposal=prop))
    assert d.verdict == "approve"
    assert d.sized_trade.leverage > 0
    assert d.sized_trade.liq_price > 100.0  # short liquidates ABOVE entry


def test_min_notional_vetoes_subminimum_trade():
    spec = SymbolSpec(
        symbol="BTCUSDT", tick_size=0.1, step_size=0.001, min_notional=1_000_000.0,
        mmr_brackets=[
            MmrBracket(
                notional_floor=0, notional_cap=50_000, mmr=0.004,
                maint_amount=0.0, max_leverage=125,
            ),
        ],
    )
    d = evaluate(_inputs(spec=spec))
    assert d.verdict == "veto"
    assert "notional" in d.reason.lower()


def test_no_heat_headroom_vetoes_new_entry():
    # existing open risk already == the 10% healthy cap -> zero headroom
    existing = [dict(symbol="ETHUSDT", direction="long", qty=200.0, entry=100.0, stop=95.0)]
    d = evaluate(_inputs(open_positions=existing))
    assert d.verdict == "veto"
    assert "heat" in d.reason.lower()


# ---- per-trade risk_mult: a REDUCTION-ONLY override (clamped to (0,1]) so the team can size an
# unproven-edge starter smaller. Provably can never increase risk / weaken a limit.

def _approved_qty(**prop_over):
    d = evaluate(_inputs(proposal=_proposal(**prop_over)))
    assert d.verdict in ("approve", "resize"), d.reason
    return d.sized_trade.qty


def test_risk_mult_half_halves_qty():
    # half risk_mult -> half the size (same entry/stop/regime), since dollar risk = eq*risk_pct
    full = _approved_qty()
    half = evaluate(_inputs(proposal=_proposal()  # baseline risk_mult defaults to 1.0
                            .model_copy(update={"risk_mult": 0.5}))).sized_trade.qty
    assert abs(half - 0.5 * full) < 1e-9


def test_risk_mult_default_is_unchanged():
    # default (no risk_mult) must be identical to an explicit 1.0 — zero behavior change
    base = _approved_qty()
    explicit_one = evaluate(_inputs(proposal=_proposal().model_copy(update={"risk_mult": 1.0}))
                            ).sized_trade.qty
    assert abs(base - explicit_one) < 1e-12


def test_risk_mult_above_one_clamped_to_one():
    # >1 must be CLAMPED (can NEVER increase risk above the policy cap / weaken a limit)
    base = _approved_qty()
    over = evaluate(_inputs(proposal=_proposal().model_copy(update={"risk_mult": 5.0}))
                    ).sized_trade.qty
    assert abs(over - base) < 1e-12


def test_risk_mult_zero_or_negative_never_increases_risk():
    # degenerate values must never blow up size: 0 -> treated as full (1.0) NOT infinite; negative
    # -> clamped to 0 -> qty 0 -> vetoed. Either way risk never exceeds the cap.
    base = _approved_qty()
    zero = evaluate(_inputs(proposal=_proposal().model_copy(update={"risk_mult": 0.0})))
    assert zero.verdict in ("approve", "resize") and abs(zero.sized_trade.qty - base) < 1e-12
    neg = evaluate(_inputs(proposal=_proposal().model_copy(update={"risk_mult": -0.5})))
    assert neg.verdict == "veto"  # negative -> 0 risk -> zero qty -> safe veto


def test_hard_min_rr_constant():
    assert HARD_MIN_RR == 1.6 and MIN_RR == 2.0


def test_gate_uses_adaptive_floor_below_default():
    p = _proposal(tps=(108.5,))                    # RR (108.5-100)/(100-95) = 1.7
    assert evaluate(_inputs(proposal=p, rr_floor=None)).verdict == "veto"   # None -> 2.0
    assert evaluate(_inputs(proposal=p, rr_floor=1.6)).verdict in ("approve", "resize")


def test_gate_hard_min_wraps_corrupt_floor():
    p = _proposal(tps=(107.5,))                    # RR 1.5
    d = evaluate(_inputs(proposal=p, rr_floor=0.5))   # hostile floor wrapped up to HARD_MIN_RR 1.6
    assert d.verdict == "veto" and "RR" in d.reason


def test_gate_default_rr_floor_unchanged_at_2():
    p = _proposal(tps=(110.0,))                    # RR 2.0 exactly -> passes at default
    assert evaluate(_inputs(proposal=p)).verdict in ("approve", "resize")
    p17 = _proposal(tps=(108.5,))                  # RR 1.7 -> vetoed at default 2.0
    assert evaluate(_inputs(proposal=p17)).verdict == "veto"


# --------------------------------------------------- open-air-TP RR guard (structure-capped RR)

def test_open_air_long_tp_beyond_swing_high_is_vetoed():
    # ZEC-type fabrication: a NOW-entry long whose only resistance (swingH 108) sits BETWEEN entry
    # (100) and a phantom TP (130). Raw RR = 30/5 = 6 (passes the floor), but RR to the REAL
    # resistance is only (108-100)/5 = 1.6 < 2.0 -> the gate must veto on the structure-capped RR.
    p = _proposal(entry=100.0, stop=95.0, tps=(130.0,))
    d = evaluate(_inputs(proposal=p, swing_high=108.0))
    assert d.verdict == "veto"
    assert "open-air" in d.reason.lower() or "structure" in d.reason.lower()


def test_open_air_short_tp_beyond_swing_low_is_vetoed():
    # Mirror: short entry 100, stop 105 (risk 5), support swingL 92 BETWEEN a phantom TP (70) and
    # entry. Raw RR = 30/5 = 6; RR to real support = (100-92)/5 = 1.6 < 2.0 -> veto.
    p = _proposal(direction="short", entry=100.0, stop=105.0, tps=(70.0,))
    d = evaluate(_inputs(proposal=p, spec=_spec().model_copy(update={"min_notional": 1.0}),
                         swing_low=92.0))
    assert d.verdict == "veto"
    assert "open-air" in d.reason.lower() or "structure" in d.reason.lower()


def test_breakout_long_entry_above_swing_high_is_not_capped():
    # A breakout enters AT/above the broken swing_high (99 < entry 100), legitimately targeting a
    # measured move beyond it (TP 115). No swing sits BETWEEN entry and TP -> no cap -> raw RR 3 ok.
    p = _proposal(entry=100.0, stop=95.0, tps=(115.0,))
    d = evaluate(_inputs(proposal=p, swing_high=99.0))
    assert d.verdict in ("approve", "resize")


def test_breakdown_short_entry_below_swing_low_is_not_capped():
    # Mirror breakdown: short enters AT/below the broken swing_low (101 > entry 100), TP 85 beyond.
    # swing_low is not strictly between TP and entry -> no cap -> raw RR 3 passes.
    p = _proposal(direction="short", entry=100.0, stop=105.0, tps=(85.0,))
    d = evaluate(_inputs(proposal=p, spec=_spec().model_copy(update={"min_notional": 1.0}),
                         swing_low=101.0))
    assert d.verdict in ("approve", "resize")


def test_tp_short_of_swing_uses_raw_rr():
    # Conservative TP (110) BELOW the swing_high (120): the swing is not between entry and TP, so no
    # cap applies and the raw RR (2.0) stands -> passes.
    p = _proposal(entry=100.0, stop=95.0, tps=(110.0,))
    d = evaluate(_inputs(proposal=p, swing_high=120.0))
    assert d.verdict in ("approve", "resize")


def test_none_swings_are_byte_identical_no_guard():
    # The exact ZEC-type proposal that the guard vetoes, with swings unsupplied (None) -> dormant ->
    # raw RR 6 passes. Proves the guard is the ONLY behavioral change vs today.
    p = _proposal(entry=100.0, stop=95.0, tps=(130.0,))
    assert evaluate(_inputs(proposal=p)).verdict in ("approve", "resize")


def test_now_entry_capped_rr_clears_floor_is_not_vetoed():
    # No false-veto: swingH 111 sits between entry (100) and TP (130), but the capped RR
    # (11/5 = 2.2) still clears the 2.0 floor -> the guard must NOT veto.
    p = _proposal(entry=100.0, stop=95.0, tps=(130.0,))
    d = evaluate(_inputs(proposal=p, swing_high=111.0))
    assert d.verdict in ("approve", "resize")


def test_swing_within_atr_margin_is_a_breakout_not_capped():
    # A market long at the current high: swingH 100.5 is only 0.25 ATR (ATR=2.0) above entry 100 ->
    # de-facto breakout, NOT an intervening obstacle -> no cap -> raw RR 6 passes. (This is the
    # market-entry-at-the-recent-high case that must not be spuriously vetoed.)
    p = _proposal(entry=100.0, stop=95.0, tps=(130.0,))   # atr defaults to 2.0
    d = evaluate(_inputs(proposal=p, swing_high=100.5))
    assert d.verdict in ("approve", "resize")


def test_zero_atr_proposal_keeps_guard_dormant_no_false_veto():
    # Degraded feed: a market proposal with atr=0 cannot size the de-facto-breakout margin, so the
    # guard stays dormant (falls back to the raw-RR floor) rather than risk a false veto on a swing
    # a hair above entry. Phantom TP 130 with swingH 100.01 -> raw RR 6 passes, NOT vetoed.
    p = _proposal(entry=100.0, stop=95.0, tps=(130.0,)).model_copy(update={"atr": 0.0})
    d = evaluate(_inputs(proposal=p, swing_high=100.01))
    assert d.verdict in ("approve", "resize")
    import futures_fund.risk_gate as rg
    assert rg._structure_capped_reward_risk(p, 108.0, None) is None   # atr=0 -> dormant (None)


def test_structure_capped_rr_helper_strengthen_only_and_none_cases():
    import futures_fund.risk_gate as rg
    # cap fires (swing between) and is <= the raw RR (strengthen-only)
    p = _proposal(entry=100.0, stop=95.0, tps=(130.0,))
    capped = rg._structure_capped_reward_risk(p, swing_high=108.0, swing_low=None)
    assert capped == pytest.approx(1.6) and capped <= rg._reward_risk(p)
    # no swing supplied -> None (dormant)
    assert rg._structure_capped_reward_risk(p, swing_high=None, swing_low=None) is None
    # breakout (entry above swing) -> None
    assert rg._structure_capped_reward_risk(p, swing_high=99.0, swing_low=None) is None
    # short mirror caps to swing_low
    ps = _proposal(direction="short", entry=100.0, stop=105.0, tps=(70.0,))
    assert rg._structure_capped_reward_risk(ps, None, 92.0) == pytest.approx(1.6)

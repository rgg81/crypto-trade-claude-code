from __future__ import annotations

from pydantic import BaseModel, Field

from futures_fund.costs import project_funding, round_trip_fee
from futures_fund.liquidation import liquidation_price, mmr_for_notional
from futures_fund.models import (
    CostEstimate,
    PortfolioHealth,
    RegimeState,
    RiskDecision,
    SizedTrade,
    SymbolSpec,
    TradeProposal,
)
from futures_fund.policy import caps_for, circuit_breaker
from futures_fund.portfolio_risk import position_risk
from futures_fund.sizing import choose_leverage, liq_distance_ratio, qty_from_risk

MIN_RR = 2.0
_RR_EPS = 1e-6  # float tolerance so an exactly-2R proposal isn't vetoed by rounding
HARD_MIN_RR = 1.6  # gate-owned ABSOLUTE floor: an adaptive rr_floor never drops a veto below this
MIN_LIQ_DISTANCE_MULT = 2.5
# A swing counts as a genuine intervening obstacle (open-air-TP guard) only if it sits more than
# this many ATR beyond entry. A swing within the margin is effectively AT the entry (price at its
# high/low -> a de-facto break targeting a measured move), which must NOT be capped.
_OPEN_AIR_STRUCT_MARGIN_ATR = 0.5


class GateInputs(BaseModel):
    proposal: TradeProposal
    spec: SymbolSpec
    regime: RegimeState
    health: PortfolioHealth
    open_positions: list[dict] = Field(default_factory=list)
    daily_pnl_pct: float = 0.0
    weekly_pnl_pct: float = 0.0
    monthly_pnl_pct: float = 0.0
    pay_bnb: bool = False
    rr_floor: float | None = None  # regime-adaptive RR floor; None -> MIN_RR. HARD_MIN_RR-wrapped.
    # Nearest structural resistance/support (brief swing_levels) for the open-air-TP guard. None ->
    # the guard is dormant (byte-identical to pre-guard behavior).
    swing_high: float | None = None
    swing_low: float | None = None


def _reward_risk(p: TradeProposal) -> float:
    if not p.take_profits:
        return 0.0
    nearest_tp = min(p.take_profits, key=lambda tp: abs(tp - p.entry))
    reward = abs(nearest_tp - p.entry)
    risk = p.risk_per_unit
    return reward / risk if risk > 0 else 0.0


def _structure_capped_reward_risk(p: TradeProposal, swing_high: float | None,
                                  swing_low: float | None) -> float | None:
    """The reward:risk re-measured to the first structural level (swing) that lies STRICTLY BETWEEN
    the entry and the nearest take-profit — the realistic first target the price must clear.
    Returns None when no such intervening swing exists, so the caller relies on the raw RR alone:
    a breakout (entry at/above swing_high) / breakdown (entry at/below swing_low) has the swing
    BEHIND the entry and legitimately targets a measured move beyond it (SOL cy96); a conservative
    TP short of the swing has nothing between; and a swing within `_OPEN_AIR_STRUCT_MARGIN_ATR` of
    entry is the current high/low (a de-facto break, not an obstacle). The guard fires ONLY on the
    gamed geometry — a now-entry placing its TP PAST a genuine first obstacle into open air to
    manufacture RR (ZEC/BNB cy100-105). Pure; `capped <= _reward_risk` always (a nearer reward can
    only shrink), so the caller's veto can only TIGHTEN the floor. None on no TP / risk<=0."""
    if not p.take_profits:
        return None
    risk = p.risk_per_unit
    if risk <= 0 or not p.atr or p.atr <= 0:
        return None   # no TP / no risk / no ATR to size the breakout margin -> stay dormant
    margin = _OPEN_AIR_STRUCT_MARGIN_ATR * p.atr
    nearest_tp = min(p.take_profits, key=lambda tp: abs(tp - p.entry))
    if p.direction == "long":
        if swing_high is None or swing_high >= nearest_tp or swing_high - p.entry <= margin:
            return None
        return (swing_high - p.entry) / risk
    if swing_low is None or swing_low <= nearest_tp or p.entry - swing_low <= margin:
        return None
    return (p.entry - swing_low) / risk


def _build_sized(p: TradeProposal, spec: SymbolSpec, qty: float, leverage: float) -> SizedTrade:
    notional = qty * p.entry
    mmr, maint = mmr_for_notional(notional, spec.mmr_brackets)
    margin = notional / leverage if leverage > 0 else notional
    liq = liquidation_price(p.entry, qty, margin, p.direction, mmr, maint)
    fees = round_trip_fee(notional, maker_entry=False, maker_exit=False)
    # Per-contract funding interval (Binance uses 4h for many perps, 1h under stress);
    # not the magic 8.
    n_events = max(1, int(p.horizon_hours // p.funding_interval_hours))
    funding = project_funding(notional, p.funding_rate, p.direction, n_events=n_events)
    # Slippage is left 0.0 in A1 (no live L2 book); A2/A3 wires slippage_cost + tick/step rounding.
    cost = CostEstimate(entry_fee=fees / 2, exit_fee=fees / 2, funding=max(0.0, funding))
    return SizedTrade(proposal=p, qty=qty, notional=notional, leverage=leverage,
                      margin=margin, liq_price=liq, cost=cost)


def evaluate(inp: GateInputs) -> RiskDecision:
    p, spec = inp.proposal, inp.spec
    caps = caps_for(inp.regime, inp.health)
    breaker = circuit_breaker(inp.daily_pnl_pct, inp.weekly_pnl_pct,
                              inp.monthly_pnl_pct, inp.health.drawdown_from_peak)
    warnings: list[str] = []

    # 1. Hard stops: bias flat / breakers / zero risk budget
    if caps.bias == "flat" or caps.per_trade_risk_pct <= 0:
        return RiskDecision(verdict="veto",
                            reason=f"risk-off: regime/health forces flat (tier={inp.health.tier})")
    if not breaker.allow_new_entries:
        return RiskDecision(verdict="veto", reason=f"circuit breaker: {breaker.reason}")

    # 2. Reward:risk — regime-adaptive floor, but NEVER below the gate-owned HARD_MIN_RR (a corrupt
    #    or hostile rr_floor can only ever RAISE the floor, never breach the absolute safety bound).
    rr = _reward_risk(p)
    floor = max(inp.rr_floor if inp.rr_floor is not None else MIN_RR, HARD_MIN_RR)
    if rr < floor - _RR_EPS:
        return RiskDecision(verdict="veto", reason=f"RR {rr:.2f} < min {floor:.2f}")
    # 2b. Open-air-TP guard: a TP placed PAST the first real structure (a swing between entry+TP)
    #     can manufacture a passing RR to a vacuum. Re-measure RR to that structure and re-apply the
    #     same floor. Strengthen-only (capped <= rr); dormant when no swing is between (breakouts,
    #     conservative TPs, or no swing supplied) -> never weakens the limit, only tightens it.
    capped_rr = _structure_capped_reward_risk(p, inp.swing_high, inp.swing_low)
    if capped_rr is not None and capped_rr < floor - _RR_EPS:
        return RiskDecision(
            verdict="veto",
            reason=f"open-air TP: RR-to-structure {capped_rr:.2f} < {floor:.2f} (TP beyond swing)")

    # 3. Effective per-trade risk budget (caps × breaker multiplier × optional per-trade reduction)
    # Caution tier (caps already halved) AND the -5% step-down can both apply on the same
    # drawdown — the compounding de-risk is intentional (survival-first).
    # risk_mult is an OPTIONAL per-trade REDUCTION (e.g. half-size an unproven-edge/confirmation
    # starter). CLAMPED to (0, 1] so it can ONLY ever SHRINK a position — it can never increase risk
    # above the policy cap or weaken any limit/breaker. None/0 -> 1.0 (no-op); >1 -> 1.0; <0 -> 0.
    rm = min(1.0, max(0.0, getattr(p, "risk_mult", 1.0) or 1.0))
    risk_pct = caps.per_trade_risk_pct * breaker.risk_multiplier * rm

    # 4. Heat headroom: total open risk vs cap. Conservative — total heat >= any single
    #    correlation cluster's heat, so no unsafe trade slips through. Cluster-aware capping
    #    (treating correlated positions as one) is the Portfolio Manager's job (stage 6, A3).
    equity = inp.health.equity
    used_heat = sum(position_risk(x["qty"], x["entry"], x["stop"], equity, x.get("direction"))
                    for x in inp.open_positions)
    headroom = max(0.0, caps.max_heat - used_heat)
    if headroom <= 0:
        return RiskDecision(
            verdict="veto",
            reason=f"no heat headroom (used {used_heat:.3f} >= cap {caps.max_heat:.3f})",
        )
    effective_risk_pct = min(risk_pct, headroom)
    if effective_risk_pct < risk_pct:
        warnings.append(f"risk trimmed to heat headroom {headroom:.3f}")

    # 5. Size, leverage (output), liq distance
    qty = qty_from_risk(equity, effective_risk_pct, p.entry, p.stop)
    if qty <= 0:
        return RiskDecision(verdict="veto", reason="computed qty is zero")
    notional = qty * p.entry
    mmr, maint = mmr_for_notional(notional, spec.mmr_brackets)
    leverage = choose_leverage(p.entry, p.stop, qty, p.direction, mmr, maint,
                               caps.max_leverage, MIN_LIQ_DISTANCE_MULT)
    if leverage <= 0:
        return RiskDecision(verdict="veto",
                            reason="cannot satisfy liq-distance rule within leverage cap")

    # 6. min-notional check
    if notional < spec.min_notional:
        return RiskDecision(verdict="veto",
                            reason=f"notional {notional:.2f} < min {spec.min_notional}")

    sized = _build_sized(p, spec, qty, leverage)

    # 7. Final liq-distance assertion
    ratio = liq_distance_ratio(p.entry, p.stop, sized.liq_price, p.direction)
    if ratio < MIN_LIQ_DISTANCE_MULT - 1e-6:
        return RiskDecision(verdict="veto",
                            reason=f"liq distance {ratio:.2f}x < {MIN_LIQ_DISTANCE_MULT}x")

    verdict = "resize" if warnings else "approve"
    reason = "approved" if verdict == "approve" else "; ".join(warnings)
    return RiskDecision(verdict=verdict, reason=reason, sized_trade=sized, warnings=warnings)

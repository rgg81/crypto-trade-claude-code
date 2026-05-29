from __future__ import annotations

from futures_fund.models import SizedTrade
from futures_fund.policy import cvar
from futures_fund.portfolio_risk import position_risk


def cvar_risk_multiplier(recent_returns: list[float], threshold: float = -0.05,
                         floor: float = 0.5) -> float:
    """1.0 in calm tails; `floor` when CVaR breaches `threshold` (portfolio-level de-risk)."""
    if not recent_returns:
        return 1.0
    return floor if cvar(recent_returns) < threshold else 1.0


def _scale(st: SizedTrade, factor: float) -> SizedTrade:
    return st.model_copy(update={
        "qty": st.qty * factor,
        "notional": st.notional * factor,
        "margin": st.margin * factor,
    })


def consolidate(
    approved: list[SizedTrade], equity: float, max_heat: float,
    cvar_mult: float = 1.0, min_risk_frac: float = 0.001,
) -> list[SizedTrade]:
    """Turn the per-symbol approved trades into a final book: apply the portfolio-level CVaR
    de-risk, scale the batch down to the gross-heat cap, then drop dust positions.

    Gross heat here is the conservative sum of per-trade risk (>= any single correlation
    cluster's heat), so no unsafe book slips through. Cluster-aware refinement (treating
    correlated trades as one) is available via portfolio_risk.cluster_heat for Phase B's PM."""
    trades = [_scale(t, cvar_mult) for t in approved] if cvar_mult != 1.0 else list(approved)

    def risk(t: SizedTrade) -> float:
        return position_risk(t.qty, t.proposal.entry, t.proposal.stop, equity)

    total = sum(risk(t) for t in trades)
    if total > max_heat and total > 0:
        factor = max_heat / total
        trades = [_scale(t, factor) for t in trades]

    return [t for t in trades if risk(t) >= min_risk_frac]

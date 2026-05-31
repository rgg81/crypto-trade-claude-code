from __future__ import annotations

from collections import defaultdict

from futures_fund.equity_log import equity_series, returns_series
from futures_fund.graduation import deflated_sharpe_pvalue, graduation_verdict
from futures_fund.journal import read_all_decisions
from futures_fund.metrics import (
    agent_attribution,
    hit_rate,
    max_drawdown,
    profit_factor,
    sharpe,
    sortino,
    trial_sharpe_std,
)


def build_scorecard(state_dir, memory_dir, monthly_target: float = 0.05,
                    min_cycles: int = 20, horizon_cycles: int = 120) -> dict:
    """The desk's statistical self-portrait — injected into EVERY agent prompt so the team
    reasons WITH its measured track record (equity, return vs target, drawdown, risk-adjusted
    returns, per-agent hit-rates, graduation status, and warnings)."""
    eq = [e for _, e in equity_series(state_dir)]
    rets = returns_series(state_dir)
    closed = [d for d in read_all_decisions(memory_dir) if d.get("realized_pnl") is not None]
    n_cycles = len(eq)

    if not eq:
        return {"equity": None, "monthly_target": monthly_target, "n_cycles": 0, "n_closed": 0,
                "sharpe": 0.0, "sortino": 0.0, "max_drawdown": 0.0, "hit_rate": 0.0,
                "profit_factor": 0.0, "period_return": 0.0, "agent_hit_rates": {},
                "graduation": graduation_verdict(
                    0, 0.0, 0.0, False, 0.0,
                    min_cycles=min_cycles, horizon_cycles=horizon_cycles),
                "warnings": ["no equity history yet — desk is cold-starting"]}

    period_return = eq[-1] / eq[0] - 1.0
    mdd = max_drawdown(eq)
    shp = sharpe(rets)
    # Cross-trial Sharpe dispersion (sigma_SR) from per-symbol return streams — each symbol the
    # desk selected to trade is a "trial". None at cold-start (sparse) -> single-strategy reduction.
    per_symbol: dict[str, list[float]] = defaultdict(list)
    for d in closed:
        notional = (d.get("size") or 0.0) * (d.get("entry") or 0.0)
        if notional > 0:
            per_symbol[d["symbol"]].append(d["realized_pnl"] / notional)
    sigma_sr = trial_sharpe_std(list(per_symbol.values()))
    # conservative fixed trial count (not cycle count)
    dsr = deflated_sharpe_pvalue(rets, num_trials=10, sigma_sr=sigma_sr)
    beats_baseline = period_return > 0  # vs flat cash; a price baseline can refine this later
    grad = graduation_verdict(n_cycles, shp, dsr, beats_baseline, mdd,
                              min_cycles=min_cycles, horizon_cycles=horizon_cycles)
    attr = agent_attribution(closed)
    hr = hit_rate(closed)

    warnings: list[str] = []
    if mdd >= 0.05:
        warnings.append(f"in drawdown: {mdd:.0%} from peak — bias risk-off")
    if n_cycles >= 11 and dsr < 0.95:  # DSR only computable at >=10 returns
        warnings.append("edge not statistically proven (DSR < 0.95) — size conservatively")
    if n_cycles >= 6 and period_return < monthly_target * (n_cycles / 180.0):
        warnings.append(f"running below the {monthly_target:.0%}/mo target — do not force trades")

    return {
        "equity": eq[-1], "monthly_target": monthly_target, "n_cycles": n_cycles,
        "n_closed": len(closed), "period_return": period_return,
        "sharpe": shp, "sortino": sortino(rets), "max_drawdown": mdd,
        "hit_rate": hr, "profit_factor": profit_factor(closed),
        "dsr_pvalue": dsr,
        "agent_hit_rates": {a: round(r["hit_rate"], 3) for a, r in attr.items()},
        "graduation": grad, "warnings": warnings,
    }

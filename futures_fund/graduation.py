from __future__ import annotations

from futures_fund.metrics import PERIODS_PER_YEAR, sharpe
from futures_fund.vendor.overfit_detector import deflated_sharpe_ratio

DSR_THRESHOLD = 0.95


def deflated_sharpe_pvalue(returns: list[float], num_trials: int,
                           periods_per_year: float = PERIODS_PER_YEAR,
                           sigma_sr: float | None = None) -> float:
    """Probability the desk's Sharpe is genuinely > 0 after deflating for multiple testing
    (vendored Lopez de Prado DSR). 0.0 if < 10 observations (DSR requires backtest_length >= 10).

    sigma_sr = cross-trial Sharpe dispersion (per-period units) from tracked per-trial Sharpes;
    None falls back to the single-strategy reduction (sigma_sr = the Sharpe's standard error)."""
    if len(returns) < 10:
        return 0.0
    observed = sharpe(returns, periods_per_year=1.0)
    result = deflated_sharpe_ratio(observed_sr=observed, num_trials=max(1, num_trials),
                                   backtest_length=len(returns), sigma_sr=sigma_sr)
    return float(result.dsr_pvalue)


def graduation_verdict(n_cycles: int, sharpe: float, dsr_pvalue: float, beats_baseline: bool,
                       max_dd: float, *, min_cycles: int = 20, horizon_cycles: int = 120,
                       dsr_threshold: float = DSR_THRESHOLD) -> dict:
    """Decide paper->live readiness. graduated only if ALL criteria pass; failed if past the
    verdict horizon without an edge; otherwise not_yet with the failing criteria listed."""
    reasons: list[str] = []
    if n_cycles < min_cycles:
        reasons.append(f"need >= {min_cycles} audited cycles (have {n_cycles})")
    if sharpe <= 0:
        reasons.append(f"OOS Sharpe must be > 0 (is {sharpe:.2f})")
    if dsr_pvalue < dsr_threshold:
        reasons.append(f"DSR {dsr_pvalue:.2f} < {dsr_threshold} (edge not statistically proven)")
    if not beats_baseline:
        reasons.append("must beat buy-&-hold baseline net of costs")
    if not reasons:
        return {"status": "graduated", "reasons": []}
    if n_cycles >= horizon_cycles:
        return {"status": "failed", "reasons": reasons + [
            f"verdict horizon ({horizon_cycles} cycles) reached without an edge — retire/redesign"]}
    return {"status": "not_yet", "reasons": reasons}

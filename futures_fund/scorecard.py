from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from futures_fund.equity_log import equity_series, returns_series
from futures_fund.graduation import deflated_sharpe_pvalue, graduation_verdict
from futures_fund.journal import read_all_decisions, realized_total
from futures_fund.metrics import (
    agent_attribution,
    hit_rate,
    max_drawdown,
    profit_factor,
    sharpe,
    sortino,
    trial_sharpe_std,
)


def _numeric_cycle_dirs(state_dir) -> list[int]:
    cyc = Path(state_dir) / "cycle"
    if not cyc.exists():
        return []
    return sorted((int(p.name) for p in cyc.glob("*") if p.is_dir() and p.name.isdigit()),
                  reverse=True)


def _recent_open_count(state_dir, k: int) -> int:
    """Total NEW positions opened across the last k cycles that produced a report.json."""
    total = seen = 0
    for n in _numeric_cycle_dirs(state_dir):
        rp = Path(state_dir) / "cycle" / str(n) / "report.json"
        if not rp.exists():
            continue
        try:
            total += int(json.loads(rp.read_text()).get("opened", 0) or 0)
        except (json.JSONDecodeError, OSError, ValueError, TypeError):
            pass
        seen += 1
        if seen >= k:
            break
    return total


def _latest_screen_has_candidates(state_dir) -> bool:
    """True if the most recent cycle that ran a screen surfaced >= 1 candidate (edge on the board)."""
    for n in _numeric_cycle_dirs(state_dir):
        sp = Path(state_dir) / "cycle" / str(n) / "screened.json"
        if not sp.exists():
            continue
        try:
            return len(json.loads(sp.read_text()).get("symbols", [])) > 0
        except (json.JSONDecodeError, OSError):
            return False
    return False


def _has_open_positions(state_dir) -> bool:
    p = Path(state_dir) / "positions.json"
    if not p.exists():
        return False
    try:
        return len(json.loads(p.read_text())) > 0
    except (json.JSONDecodeError, OSError, TypeError):
        return False


def _is_halted_raw(state_dir) -> bool:
    p = Path(state_dir) / "account.json"
    if not p.exists():
        return False
    try:
        return bool(json.loads(p.read_text()).get("halt", False))
    except (json.JSONDecodeError, OSError):
        return False


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
            per_symbol[d["symbol"]].append(realized_total(d) / notional)   # incl. partial banks
    sigma_sr = trial_sharpe_std(list(per_symbol.values()))
    # conservative fixed trial count (not cycle count)
    dsr = deflated_sharpe_pvalue(rets, num_trials=10, sigma_sr=sigma_sr)
    beats_baseline = period_return > 0  # vs flat cash; a price baseline can refine this later
    grad = graduation_verdict(n_cycles, shp, dsr, beats_baseline, mdd,
                              min_cycles=min_cycles, horizon_cycles=horizon_cycles)
    attr = agent_attribution(closed)
    hr = hit_rate(closed)

    # The warnings are deliberately TWO-SIDED. Three are brakes (risk-reducing); one is an
    # accelerator (counter-signal against under-deployment). Without the accelerator the injected
    # context is a one-way ratchet that talks the desk out of every clean trade — the root cause of
    # the desk standing down to cash for cycles on end. See tests/test_scorecard.py.
    warnings: list[str] = []
    # --- BRAKE: real drawdown still hard-brakes (unchanged) ---
    if mdd >= 0.05:
        warnings.append(f"in drawdown: {mdd:.0%} from peak — bias risk-off")
    # --- BRAKE: unproven edge sizes down (unchanged) ---
    if n_cycles >= 11 and dsr < 0.95:  # DSR only computable at >=10 returns
        warnings.append("edge not statistically proven (DSR < 0.95) — size conservatively")
    # --- QUALITY (reworded, two-sided): gate on a TRAILING-window pace, not the old cumulative-
    # from-inception bar that latched permanently once underwater (and NOT on the small-sample
    # hit-rate). Demands quality WITHOUT mandating passivity. ---
    window = min(len(rets), 30)
    trailing = sum(rets[-window:]) if window else 0.0
    if n_cycles >= 6 and trailing < monthly_target * (window / 180.0):
        warnings.append(
            f"below the {monthly_target:.0%}/mo pace — require a clean, proven-edge setup before "
            "sizing up and do NOT chase or force low-quality trades; but a qualifying setup that "
            "clears RR>=2 and the heat cap is NOT forcing — do not stand flat on it")
    # --- ACCELERATOR (counter-signal): opportunity cost of idle cash. Fires ONLY in a tradeable
    # state — healthy, not halted, FLAT, zero opens over the last 2 cycles, AND the screen still
    # surfacing candidates — and self-silences the moment the desk is deployed, in drawdown,
    # halted, or the board is genuinely empty, so it can never manufacture trades in a thin tape. ---
    eq_peak = max(eq)
    cur_dd = (eq_peak - eq[-1]) / eq_peak if eq_peak > 0 else 0.0
    if (n_cycles >= 6 and cur_dd < 0.05 and not _is_halted_raw(state_dir)
            and not _has_open_positions(state_dir)
            and _recent_open_count(state_dir, k=2) == 0
            and _latest_screen_has_candidates(state_dir)):
        warnings.append(
            "under-deployed: FLAT with zero new opens across the last 2 cycles while the screen "
            f"keeps surfacing candidates — idle cash has opportunity cost vs the {monthly_target:.0%}"
            "/mo target. Standing flat is itself a position with negative carry; do NOT stand flat "
            "on a clean, edge-aligned setup that clears the gate (RR>=2 + heat). Taking such a "
            "setup is NOT forcing.")

    return {
        "equity": eq[-1], "monthly_target": monthly_target, "n_cycles": n_cycles,
        "n_closed": len(closed), "period_return": period_return,
        "sharpe": shp, "sortino": sortino(rets), "max_drawdown": mdd,
        "hit_rate": hr, "profit_factor": profit_factor(closed),
        "dsr_pvalue": dsr,
        "agent_hit_rates": {a: round(r["hit_rate"], 3) for a, r in attr.items()},
        "graduation": grad, "warnings": warnings,
    }

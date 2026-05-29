from __future__ import annotations

import numpy as np

PERIODS_PER_YEAR = 2190.0  # 4h cycles: 6/day * 365


def sharpe(returns: list[float], periods_per_year: float = PERIODS_PER_YEAR) -> float:
    if len(returns) < 2:
        return 0.0
    arr = np.asarray(returns, dtype=float)
    sd = arr.std(ddof=1)
    if sd == 0:
        return 0.0
    return float(arr.mean() / sd * np.sqrt(periods_per_year))


def sortino(returns: list[float], periods_per_year: float = PERIODS_PER_YEAR) -> float:
    if len(returns) < 2:
        return 0.0
    arr = np.asarray(returns, dtype=float)
    downside = arr[arr < 0]
    dd = downside.std(ddof=1) if len(downside) >= 2 else 0.0
    if dd == 0:
        return float(arr.mean() / 1e-9 * np.sqrt(periods_per_year)) if arr.mean() > 0 else 0.0
    return float(arr.mean() / dd * np.sqrt(periods_per_year))


def max_drawdown(equity: list[float]) -> float:
    """Largest peak-to-trough decline as a positive fraction (0 if monotonic up / too short)."""
    if len(equity) < 2:
        return 0.0
    peak = equity[0]
    mdd = 0.0
    for e in equity:
        peak = max(peak, e)
        if peak > 0:
            mdd = max(mdd, (peak - e) / peak)
    return mdd


def calmar(annual_return: float, mdd: float) -> float:
    return annual_return / mdd if mdd > 0 else 0.0


def hit_rate(closed: list[dict]) -> float:
    if not closed:
        return 0.0
    wins = sum(1 for d in closed if d["realized_pnl"] > 0)
    return wins / len(closed)


def profit_factor(closed: list[dict]) -> float:
    gains = sum(d["realized_pnl"] for d in closed if d["realized_pnl"] > 0)
    losses = -sum(d["realized_pnl"] for d in closed if d["realized_pnl"] < 0)
    if losses == 0:
        return float("inf") if gains > 0 else 0.0
    return gains / losses


def agent_attribution(closed: list[dict]) -> dict[str, dict]:
    """Per-agent realized PnL, trade count, and hit-rate. A trade credits every agent in its
    `contributing_agents` list (falls back to 'unknown')."""
    out: dict[str, dict] = {}
    for d in closed:
        agents = d.get("contributing_agents") or ["unknown"]
        for a in agents:
            rec = out.setdefault(a, {"pnl": 0.0, "count": 0, "wins": 0})
            rec["pnl"] += d["realized_pnl"]
            rec["count"] += 1
            rec["wins"] += 1 if d["realized_pnl"] > 0 else 0
    for rec in out.values():
        rec["hit_rate"] = rec["wins"] / rec["count"] if rec["count"] else 0.0
    return out

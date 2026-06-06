"""Improvement metrics (Pillar 3 — IMPROVE: get sharper every 4h, and MEASURE it).

The desk already learns (reflector -> lessons -> eval harness). This adds the missing measurement:
is the desk actually getting better, and is it deploying toward 5%/mo? Three read-only signals,
surfaced each cycle so the team and the month-end meta-reflection can see the trend:

- DEPLOYMENT RATE — fraction of recent cycles that actually put risk on (opens or armed triggers).
  Directly measures the under-deployment that this whole pivot fixes; a near-zero rate is the alarm.
- CORPUS HEALTH — is the lessons corpus growing AND two-sided (enabling vs restrictive)? A one-way
  'don't' ratchet is the documented path to all-cash.
- RETURN TREND — recent-window mean return vs the prior window: improving, flat, or decaying.

Pure read-only (no mutation); fail-safe to empty on a thin/cold log.
"""
from __future__ import annotations

import json
from pathlib import Path


def deployment_rate(state_dir, last_n: int = 10) -> dict:
    """Over the last `last_n` cycle reports: fraction that deployed risk (opened a position OR armed
    a trigger). Near-zero = the under-deployment alarm. Reads state/cycle/*/report.json."""
    reports = sorted(Path(state_dir).glob("cycle/*/report.json"),
                     key=lambda p: int(p.parent.name) if p.parent.name.isdigit() else -1)
    recent = reports[-last_n:]
    n = len(recent)
    if n == 0:
        return {"deployment_rate": 0.0, "cycles": 0, "active": 0, "opens": 0}
    active = opens = 0
    for p in recent:
        try:
            r = json.loads(p.read_text())
        except (json.JSONDecodeError, OSError, ValueError):
            continue
        o = int(r.get("opened", 0) or 0)
        armed = int(r.get("triggers_armed", 0) or 0)
        opens += o
        if o > 0 or armed > 0:
            active += 1
    return {"deployment_rate": round(active / n, 3), "cycles": n, "active": active, "opens": opens}


def corpus_health(memory_dir) -> dict:
    """Lessons-corpus two-sidedness: counts by polarity + validated count. `two_sided` = the corpus
    carries BOTH enabling and restrictive lessons (not a one-way 'don't' ratchet)."""
    try:
        from futures_fund.lessons import read_lessons
        lessons = read_lessons(memory_dir)
    except Exception:  # noqa: BLE001 — advisory; never break the cycle
        return {"total": 0, "validated": 0, "enabling": 0, "restrictive": 0, "process": 0,
                "two_sided": False}
    pol = {"enabling": 0, "restrictive": 0, "process": 0}
    validated = 0
    for lz in lessons:
        pol[getattr(lz, "polarity", "restrictive")] = pol.get(getattr(lz, "polarity",
                                                                       "restrictive"), 0) + 1
        if getattr(lz, "state", "candidate") == "validated":
            validated += 1
    return {"total": len(lessons), "validated": validated, **pol,
            "two_sided": pol["enabling"] > 0 and pol["restrictive"] > 0}


def return_trend(state_dir, window: int = 8) -> dict:
    """Recent-window vs prior-window mean per-cycle return: 'improving' | 'flat' | 'decaying'."""
    try:
        from futures_fund.equity_log import returns_series
        rs = returns_series(state_dir)
    except Exception:  # noqa: BLE001
        rs = []
    if len(rs) < 4:
        return {"trend": "insufficient", "recent_mean": 0.0, "prior_mean": 0.0, "n": len(rs)}
    recent = rs[-window:]
    prior = rs[-2 * window:-window] or rs[:-window]
    rm = sum(recent) / len(recent)
    pm = (sum(prior) / len(prior)) if prior else 0.0
    eps = 1e-4
    trend = "improving" if rm > pm + eps else ("decaying" if rm < pm - eps else "flat")
    return {"trend": trend, "recent_mean": round(rm, 5), "prior_mean": round(pm, 5), "n": len(rs)}


def improvement_panel(state_dir, memory_dir, *, last_n: int = 10) -> dict:
    """Bundle the read-only improvement signals for the scorecard / meta-reflection."""
    return {"deployment": deployment_rate(state_dir, last_n=last_n),
            "corpus": corpus_health(memory_dir),
            "returns": return_trend(state_dir)}

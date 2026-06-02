"""Regime arbiter — ONE authoritative read of the tape per cycle, replacing the per-agent
re-derivation that drifted (a Watcher calling risk-off "confirmed" off stale data while the
analysts were more careful). A deterministic core (majors breadth + BTC anchor + F&G + persistence
with two-sided hysteresis) plus a symmetric, justification-gated agent override.

This is a MARKET-NEUTRAL desk: the regime NEVER gates permission to go long or short. It is a
SYMMETRIC conviction-shaper and entry-style router — a `risk_off`/`risk_on` label sizes conviction
and decides whether an entry is WITH-regime (may be at market) or COUNTER-regime (a short while not
risk_off, or a long while risk_off — must first confirm via a trigger, enforced in the orchestrator
for BOTH sides identically). `confirmed` is the advisory STRENGTH of a risk_off read, not a gate.

Fail-closed throughout: degraded feeds, missing majors, gaps, and corrupt history can WITHHOLD a
confident label (so the orchestrator requires confirmation for BOTH directions) but can NEVER
manufacture a confident regime out of thin air. The single sanctioned asymmetry is the news term
(bad>good): news_term in [-1, 0] only deepens the ADVISORY score toward risk-off (never lifts it),
so it IS a deliberate one-directional risk-off tilt — justified because bad news genuinely gaps
harder than good. It is confined to the advisory score (news-blind to the label), so it cannot gate
permission, sizing, or the counter-regime classification; its only effect is a mild risk-off lean in
the agents' read. The desk accepts this one intentional tilt; everything else is symmetric.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path

from pydantic import BaseModel, Field

from futures_fund.scheduling import floor4

_MAJORS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT"]
_CANDLE = timedelta(hours=4)

# score weights (sum 1.0); score in ~[-1, +1], NEGATIVE = risk-off
_W_BREADTH, _W_BTC, _W_FNG, _W_NEWS = 0.40, 0.35, 0.15, 0.10
# Single SYMMETRIC band magnitude (replaces the old asymmetric -0.30 / +0.25, which made risk_on
# easier to reach than risk_off — a residual long-tilt). risk_off at score_core <= -_BAND, risk_on
# at >= +_BAND, mixed between. 0.28 ~ the average of the two old thresholds, so overall regime
# sensitivity / trade-frequency is preserved while the +/- treatment is provably identical.
_BAND = 0.28
_QUORUM = 3               # >= 3 majors present AND BTC present — required for EITHER label (symmetric)


class RegimeState(BaseModel):
    regime: str                     # 'risk_on' | 'risk_off' | 'mixed' (final, post-override)
    confirmed: bool                 # ADVISORY two-sided conviction: the directional label (risk_off
                                    # OR risk_on) persisted >= K candles. NOT a permission gate.
    score: float
    drivers: dict = Field(default_factory=dict)
    candle: str = ""                # floor4(now).isoformat() — served-candle key
    cycle_no: int = 0


def _store(state_dir) -> Path:
    return Path(state_dir) / "regime_history.jsonl"


def read_regime_history(state_dir) -> list[dict]:
    """All persisted regime records; skips malformed lines, never raises. Missing file -> []."""
    p = _store(state_dir)
    if not p.exists():
        return []
    out = []
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
            if isinstance(rec, dict):
                out.append(rec)
        except (json.JSONDecodeError, ValueError):
            continue  # skip a half-written/corrupt line
    return out


def append_regime_history(state_dir, regime_state: RegimeState) -> None:
    """Append-OR-REPLACE keyed on cycle_no (idempotent under a RETRY re-running the same cycle)."""
    recs = [r for r in read_regime_history(state_dir) if r.get("cycle_no") != regime_state.cycle_no]
    recs.append({"cycle_no": regime_state.cycle_no, "candle": regime_state.candle,
                 "deterministic_regime": regime_state.drivers.get("deterministic_regime"),
                 "regime": regime_state.regime, "confirmed": regime_state.confirmed,
                 "score": regime_state.score})
    p = _store(state_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".jsonl.tmp")
    tmp.write_text("".join(json.dumps(r, default=str) + "\n" for r in recs))
    os.replace(tmp, p)


def _major_down(brief: dict) -> bool:
    td = brief.get("trend_direction")
    mom = brief.get("momentum_20", brief.get("momentum"))
    if td == "down":
        return True
    if td == "up":
        return False
    try:
        return float(mom) < 0 if mom is not None else False
    except (TypeError, ValueError):
        return False


def _deterministic(briefs: list[dict], market_context: dict, news_risk_off):
    by_id = {b.get("exchange_id"): b for b in (briefs or [])}
    present = [m for m in _MAJORS if m in by_id]
    down = [m for m in present if _major_down(by_id[m])]
    breadth = (len(down) / len(present)) if present else 0.0
    btc_present = "BTCUSDT" in by_id
    btc_down = btc_present and _major_down(by_id["BTCUSDT"])

    degraded = []
    breadth_term = 1.0 - 2.0 * breadth                       # all-down -> -1, all-up -> +1
    if btc_present:
        btc_term = -1.0 if btc_down else 1.0
    else:
        btc_term, degraded = 0.0, degraded + ["btc_absent"]
    fng = (market_context or {}).get("fear_greed")
    fng_val = fng.get("value") if isinstance(fng, dict) else fng
    try:
        fng_term = max(-1.0, min(1.0, (float(fng_val) - 50.0) / 50.0))
    except (TypeError, ValueError):
        fng_term, degraded = 0.0, degraded + ["fear_greed_missing"]   # abstain, not an extreme
    if news_risk_off is None:
        news_term, _ = 0.0, degraded.append("news_flag_missing")
    else:
        news_term = -1.0 if news_risk_off else 0.0

    # The deterministic LABEL (`det`) is computed from a NEWS-BLIND score. `news_risk_off` is an
    # LLM-judged signal (the News analyst sets it); keeping it out of the label means news can never
    # tip a near-boundary tape across the band and manufacture a regime read out of a soft signal.
    # News still colors the ADVISORY `score` the agents read (so a shock is visible), and the
    # asymmetry holds there (news_term in [-1, 0] only deepens risk-off, never lifts toward risk-on),
    # but it cannot move `det`, persistence, or conviction strength.
    score_core = _W_BREADTH * breadth_term + _W_BTC * btc_term + _W_FNG * fng_term
    score = score_core + _W_NEWS * news_term                 # advisory (reported); news-inclusive
    # quorum gates BOTH labels symmetrically (previously only risk_off required it — that let risk_on
    # be declared on thinner data, a hidden long-tilt).
    quorum = len(present) >= _QUORUM and btc_present
    if score_core <= -_BAND and quorum:                      # LABEL is news-blind
        det = "risk_off"
    elif score_core >= _BAND and quorum:
        det = "risk_on"
    else:
        det = "mixed"
    drivers = {"majors_present": present, "majors_down": down, "breadth": round(breadth, 3),
               "btc_anchor_down": btc_down, "fng": fng_val, "news_risk_off": news_risk_off,
               "score_core": round(score_core, 4), "quorum_met": quorum, "degraded": degraded}
    return det, round(score, 4), quorum, drivers


def _persistence_count(state_dir, det_regime: str, this_candle: datetime, k: int) -> int:
    """Count of the most-recent K contiguous 4h candles (this one + priors on the grid) recorded
    with the SAME directional label as `det_regime`. SYMMETRIC: it confirms durability for risk_off
    AND risk_on identically (so conviction strength is two-sided, per the market-neutral mandate);
    `mixed` has no directional durability to confirm -> 0. This cycle's just-computed label counts as
    the most recent (it is NOT yet persisted — append-after-read — so a cycle can never confirm
    itself). A gap or a different label breaks the chain. Future-dated history (clock skew) is
    discarded."""
    if det_regime not in ("risk_off", "risk_on"):
        return 0
    by_candle = {}
    for r in read_regime_history(state_dir):
        c = r.get("candle")
        if not c:
            continue
        try:
            cd = datetime.fromisoformat(c)
        except (TypeError, ValueError):
            continue
        if cd > this_candle:           # skew guard: discard future candles
            continue
        by_candle[c] = r               # latest write per candle wins (dedupe)
    count = 1                          # this cycle (the current directional label)
    cur = this_candle
    while count < k:
        cur = cur - _CANDLE
        rec = by_candle.get(cur.isoformat())
        if rec and rec.get("deterministic_regime") == det_regime:
            count += 1
        else:
            break                      # gap or non-risk_off breaks contiguity -> withhold
    return count


def classify_regime(state_dir, market_context: dict, briefs: list[dict], now: datetime, *,
                    cycle_no: int, agent_override: dict | None = None, news_risk_off=None,
                    k: int = 2) -> RegimeState:
    """Deterministic regime + symmetric (justification-gated) agent override. Never raises."""
    candle = floor4(now)
    det, score, quorum, drivers = _deterministic(briefs, market_context, news_risk_off)
    persistence = _persistence_count(state_dir, det, candle, k)
    # SYMMETRIC: a directional label (risk_off OR risk_on) that has persisted >= K candles with
    # quorum is "confirmed" — a durable-regime conviction stamp, identical for both sides.
    det_confirmed = (det in ("risk_off", "risk_on") and persistence >= k and quorum)
    drivers["persistence_count"] = persistence
    drivers["k"] = k
    drivers["deterministic_regime"] = det

    # --- SYMMETRIC override (a justified agent may force the regime label either way). It privileges
    # neither direction: forcing risk_off makes new LONGS counter-regime (must confirm via trigger);
    # forcing risk_on makes new SHORTS counter-regime. It no longer "unlocks" anything — permission
    # is unconditional; the override only shapes conviction + entry style for BOTH sides.
    final_regime = det
    ov = {"applied": False}
    if isinstance(agent_override, dict):
        just = (agent_override.get("justification") or "").strip()
        ov_regime = agent_override.get("regime")
        if just and ov_regime in ("risk_on", "mixed", "risk_off"):
            final_regime = ov_regime
            ov.update({"applied": True, "direction": ov_regime, "justification": just})
        elif ov_regime and not just:
            ov["rejected_reason"] = "override ignored: no justification"
    drivers["override"] = ov
    drivers["final_regime"] = final_regime

    # `confirmed` is ADVISORY ONLY — the durable-regime conviction strength (a two-sided input), not
    # a permission gate. It is True only when the deterministic label is confirmed AND the agent did
    # NOT override it to a different label (an agent-contested read is not "confirmed"). Counter-
    # regime entries are confirmed by a trigger in the orchestrator, symmetrically for both sides.
    confirmed = (final_regime == det) and det_confirmed
    return RegimeState(regime=final_regime, confirmed=confirmed,
                       score=score, drivers=drivers, candle=candle.isoformat(), cycle_no=cycle_no)

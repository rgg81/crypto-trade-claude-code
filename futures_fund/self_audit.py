"""Standing self-audit (Pillar 4 — AUDIT). A fast, deterministic panel of the desk's CRITICAL
cross-module invariants, runnable any cycle / on demand (scripts/self_audit_cli.py) as a
complement to the full test suite. It catches a regression in a load-bearing safety/symmetry/
pacing property without running 600+ tests. Pure-import checks; no I/O, no network.

This is the repeatable, deterministic core of "auditing to make sure no bugs" — distinct from (and
cheaper than) the heavy multi-agent adversarial review, whose own verify pass can fail. Every check
here is a hard invariant that MUST hold for the desk to be safe to run.
"""
from __future__ import annotations


def _checks() -> list[tuple[str, bool, str]]:
    out: list[tuple[str, bool, str]] = []

    def add(name, ok, detail=""):
        out.append((name, bool(ok), detail))

    # 1. ANTI-MARTINGALE: pacing must NEVER press while in drawdown (it would double into losses).
    from futures_fund.pacing import CAUTION_DD, compute_pacing
    dd_press = compute_pacing(mtd_return=-0.04, days_elapsed=15, days_in_month=30,
                              drawdown=CAUTION_DD + 0.01, open_heat=0.0).mode
    add("pacing.anti_martingale", dd_press != "press",
        f"in-drawdown mode={dd_press} (must NOT be press)")

    # 2. Pacing DOES press when genuinely behind + healthy + under-deployed (deployment works).
    behind = compute_pacing(mtd_return=0.0, days_elapsed=15, days_in_month=30,
                            drawdown=0.0, open_heat=0.0).mode
    add("pacing.presses_when_behind", behind == "press", f"behind+healthy mode={behind}")

    # 3. Pacing throttles once the target is hit.
    hit = compute_pacing(mtd_return=0.05, days_elapsed=10, days_in_month=30,
                         drawdown=0.0, open_heat=0.0).mode
    add("pacing.throttles_at_target", hit == "throttle", f"target-hit mode={hit}")

    # 4. RR FLOOR is intact (>= 2.0) — the gate's reward:risk survival floor.
    from futures_fund.risk_gate import MIN_RR
    add("gate.rr_floor>=2", MIN_RR >= 2.0, f"MIN_RR={MIN_RR}")

    # 5. ANTI-HALLUCINATION: a fabricated far-from-mark entry is dropped; a clean one kept.
    from futures_fund.proposal_audit import audit_item
    gt = {"X": {"mark": 100.0, "atr": 2.0}}
    fab = audit_item({"symbol": "X", "entry": 150.0, "atr": 2.0}, gt, is_trigger=False)[0]
    clean = audit_item({"symbol": "X", "entry": 100.5, "atr": 2.0}, gt, is_trigger=False)[0]
    add("audit.drops_fabrication", (not fab) and clean, f"fab_kept={fab} clean_kept={clean}")

    # 6. Anti-hallucination is FAIL-OPEN (never a deploy-blocker) on missing ground truth.
    failopen = audit_item({"symbol": "Z", "entry": 999.0, "atr": 999.0}, {}, is_trigger=False)[0]
    add("audit.fail_open_no_truth", failopen, "missing ground truth must keep the proposal")

    # 7. LONG/SHORT SYMMETRY: the audit verdict is direction-agnostic.
    longv = audit_item({"symbol": "X", "direction": "long", "entry": 150.0, "atr": 2.0}, gt,
                       is_trigger=False)[0]
    shortv = audit_item({"symbol": "X", "direction": "short", "entry": 150.0, "atr": 2.0}, gt,
                        is_trigger=False)[0]
    add("audit.long_short_symmetric", longv == shortv, f"long={longv} short={shortv}")

    # 8. ADAPT: the playbook routes range->mean-reversion and trend->trend-follow.
    from futures_fund.playbook import is_range, playbook_for
    _rng_pb = playbook_for("low_vol_range")["strategies"]
    rng = any("mean-reversion" in s or "fade" in s for s in _rng_pb)
    trd = any("trend" in s for s in playbook_for("high_vol_trend")["strategies"])
    add("playbook.regime_routing", rng and trd and is_range("low_vol_range"),
        f"range_mr={rng} trend_tf={trd}")

    return out


def run_self_audit() -> dict:
    """Run the invariant panel. Returns {ok, checks:[{name, ok, detail}]}; ok = all checks pass."""
    results = _checks()
    return {"ok": all(ok for _, ok, _ in results),
            "checks": [{"name": n, "ok": ok, "detail": d} for n, ok, d in results]}

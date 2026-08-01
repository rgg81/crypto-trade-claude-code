"""Phases 7-10 CLI: gate + consolidate + execute the trader proposals; persist + report.

    uv run python scripts/gate_execute_cli.py --cycle N
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime

from futures_fund import rate_limit
from futures_fund.config import load_settings
from futures_fund.cycle_io import load_output, save_output
from futures_fund.exchange import FuturesExchange
from futures_fund.orchestration import (
    funnel_skipped,
    gate_execute_step,
    management_review,
    reclassify_skipped,
)
from futures_fund.scheduling import floor4


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cycle", type=int, required=True)
    ap.add_argument("--symbols", default=None,
                    help="comma-separated unified symbols (the Watcher's picks); overrides "
                         "config. Held positions are folded in automatically.")
    ap.add_argument("--weight-threshold", type=int, default=rate_limit.DEFAULT_THRESHOLD,
                    help="run only when the IP's used weight is under this (0 disables)")
    args = ap.parse_args()
    settings = load_settings()
    # explicit --symbols (even empty) is the Watcher's universe for this cycle; never the default
    if args.symbols is not None:
        syms = [s.strip() for s in args.symbols.split(",") if s.strip()]
        settings = settings.model_copy(update={"symbols": syms})
    # cy317: wait for real IP-weight headroom BEFORE from_settings (which runs
    # load_markets). A weight-1 probe proves no ACTIVE ban but is blind to a
    # drained budget — the state that makes the next burst trip a fresh 418.
    rate_limit.guard(settings, threshold=args.weight_threshold)
    ex = FuturesExchange.from_settings(settings)
    payload = load_output("state", args.cycle, "proposals")
    # The agent path ALWAYS carries a holdings review (possibly empty). A missing/null management
    # key must NEVER reach the gate as None — that would close the whole book by absence on a
    # stand-down/HALT. Coerce to an empty review (keep holdings) and surface the anomaly.
    if payload.get("management") is None:
        print("WARNING: proposals.json has no 'management' key — treating as an empty holdings "
              "review (holdings KEPT, not closed by absence).", file=sys.stderr)
    management = management_review(payload)
    # regime_state (the SYMMETRIC conviction + entry-style shaper) is classified in preflight ->
    # context.json; the `triggers` list (resting conditional orders) rides alongside proposals.
    # FAIL-CLOSED at this production boundary: if context.json is missing/stale OR carries no
    # regime_state, substitute a DEGRADED sentinel (no quorum) rather than None. None would
    # pass-through to a naked MARKET entry; the degraded sentinel routes BOTH directions through a
    # confirmation trigger — a never-read tape can never open a naked position (mirror-symmetric).
    _DEGRADED = {"regime": "mixed", "confirmed": False,
                 "drivers": {"quorum_met": False, "degraded": ["context_missing"]}}
    # Pillar 4 AUDIT — build the anti-hallucination ground truth {raw symbol: {mark, atr}} from the
    # context briefs so the gate can drop any proposal/trigger whose entry/atr was fabricated.
    ground_truth: dict = {}
    try:
        _ctx = load_output("state", args.cycle, "context")
        regime_state = _ctx.get("regime_state") or _DEGRADED
        for b in _ctx.get("briefs", []):
            sym = b.get("exchange_id")
            if sym:
                ground_truth[sym] = {"mark": b.get("last_close"), "atr": b.get("atr")}
    except FileNotFoundError:
        print("WARNING: context.json missing — regime UNREAD; fail-closed (both directions must "
              "confirm via trigger, no naked market entry).", file=sys.stderr)
        regime_state = _DEGRADED
    # FAIL-LOUD: a SKIPPED reclassify (Phase 4.6) must not run the gate with a news-blind regime.
    # If the analysts produced a news judgement but the news-fold was never applied, BLOCK and tell
    # the orchestrator to run reclassify first (the candle is left UNSERVED -> the next due_check
    # returns DUE RETRY, so the self-healing poll re-runs it correctly).
    try:
        analyst_reports = load_output("state", args.cycle, "analyst_reports")
    except FileNotFoundError:
        analyst_reports = None
    # FAIL-LOUD: a cycle that submits TRADES but has NO analyst_reports.json skipped the whole
    # analyst funnel (Phases 4-4.6). Block so it can't execute on a news-blind preflight regime; the
    # candle stays UNSERVED -> next due_check returns DUE RETRY and the poll re-runs it correctly.
    if funnel_skipped(analyst_reports, payload.get("proposals"), payload.get("triggers")):
        print(f"ERROR: cycle {args.cycle} submits trades (proposals/triggers) but "
              f"analyst_reports.json is MISSING — the analyst pass / screen / reclassify (Phases "
              f"4-4.6) were SKIPPED. Run the full funnel (4 analysts -> screen -> reclassify) "
              f"first. A genuine stand-down must submit EMPTY proposals AND triggers.",
              file=sys.stderr)
        raise SystemExit(2)
    if reclassify_skipped(regime_state, analyst_reports):
        print(f"ERROR: reclassify (Phase 4.6) was SKIPPED for cycle {args.cycle} — analyst_reports "
              f"carry a news risk_off judgement but regime_state.drivers.news_risk_off was never "
              f"folded (news-blind regime). Run:\n"
              f"  uv run python scripts/reclassify_cli.py --cycle {args.cycle}\n"
              f"then re-run the gate.", file=sys.stderr)
        raise SystemExit(2)
    triggers = payload.get("triggers") or []
    cancel_triggers = payload.get("cancel_triggers") or []  # team retires decayed armed triggers
    now = datetime.now(UTC)  # gate-START instant: stamps the SERVED CANDLE for the due-gate
    report = gate_execute_step(ex, settings, "state", "memory",
                               now=now, cycle_no=args.cycle,
                               proposals=payload.get("proposals", []),
                               management=management, regime_state=regime_state, triggers=triggers,
                               cancel_triggers=cancel_triggers, ground_truth=ground_truth)
    # Run-markers consumed by scripts/due_check.py (hourly-poll candle gate). candle = the 4h
    # candle this cycle served (floor of the gate-start), ran_at = audit/clock-skew sentinel.
    report["ran_at"] = now.isoformat()
    report["candle"] = floor4(now).isoformat()
    save_output("state", args.cycle, "report", report)
    print(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    main()

"""Phases 7-10 CLI: gate + consolidate + execute the trader proposals; persist + report.

    uv run python scripts/gate_execute_cli.py --cycle N
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime

from futures_fund.config import load_settings
from futures_fund.cycle_io import load_output, save_output
from futures_fund.exchange import FuturesExchange
from futures_fund.orchestration import gate_execute_step, management_review
from futures_fund.scheduling import floor4


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cycle", type=int, required=True)
    ap.add_argument("--symbols", default=None,
                    help="comma-separated unified symbols (the Watcher's picks); overrides "
                         "config. Held positions are folded in automatically.")
    args = ap.parse_args()
    settings = load_settings()
    # explicit --symbols (even empty) is the Watcher's universe for this cycle; never the default
    if args.symbols is not None:
        syms = [s.strip() for s in args.symbols.split(",") if s.strip()]
        settings = settings.model_copy(update={"symbols": syms})
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
    try:
        regime_state = load_output("state", args.cycle, "context").get("regime_state") or _DEGRADED
    except FileNotFoundError:
        print("WARNING: context.json missing — regime UNREAD; fail-closed (both directions must "
              "confirm via trigger, no naked market entry).", file=sys.stderr)
        regime_state = _DEGRADED
    triggers = payload.get("triggers") or []
    cancel_triggers = payload.get("cancel_triggers") or []  # team retires decayed armed triggers
    now = datetime.now(UTC)  # gate-START instant: stamps the SERVED CANDLE for the due-gate
    report = gate_execute_step(ex, settings, "state", "memory",
                               now=now, cycle_no=args.cycle,
                               proposals=payload.get("proposals", []),
                               management=management, regime_state=regime_state, triggers=triggers,
                               cancel_triggers=cancel_triggers)
    # Run-markers consumed by scripts/due_check.py (hourly-poll candle gate). candle = the 4h
    # candle this cycle served (floor of the gate-start), ran_at = audit/clock-skew sentinel.
    report["ran_at"] = now.isoformat()
    report["candle"] = floor4(now).isoformat()
    save_output("state", args.cycle, "report", report)
    print(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    main()

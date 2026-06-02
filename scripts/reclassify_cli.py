"""Phase 4.6 CLI: re-classify the regime AFTER the analyst pass, folding the News analyst's
risk_off_flag into the deterministic news term (which preflight, running before the analysts,
could not see). Overwrites state/cycle/N/context.json's `regime_state` in place so the debate,
Trader, and gate all read the news-informed regime.

    uv run python scripts/reclassify_cli.py --cycle N

Idempotent: re-runs reproduce the same regime_state (the persistence record is keyed by cycle_no).
Fail-safe: if analyst_reports.json is absent or the news pass is empty, the regime keeps its
preflight value unchanged (news stays degraded — today's behavior).
"""
from __future__ import annotations

import argparse
import json
import sys

from futures_fund.cycle_io import load_output, save_output
from futures_fund.orchestration import reclassify_step


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cycle", type=int, required=True)
    args = ap.parse_args()
    context = load_output("state", args.cycle, "context")
    try:
        analyst_reports = load_output("state", args.cycle, "analyst_reports")
    except FileNotFoundError:
        analyst_reports = []  # no analyst pass on disk -> news stays degraded (None), regime kept
        print("WARNING: no analyst_reports.json — regime keeps its preflight (news-degraded) value.",
              file=sys.stderr)
    regime_state = reclassify_step("state", context, analyst_reports)
    context["regime_state"] = regime_state
    save_output("state", args.cycle, "context", context)
    print(json.dumps(regime_state, indent=2, default=str))


if __name__ == "__main__":
    main()

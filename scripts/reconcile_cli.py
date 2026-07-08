"""Balance-vs-journal reconciliation + scale-out backfill (cy94 +$87 residual tool).

Re-derives the account balance from the closed-trade journal (seed - entry_fees + realized_final +
scale_out_banks) and surfaces the residual. Scans the cycle reports for reduce/scale-out banks and
flags any NOT recorded in the journal's `partial_banks` (the cy22 SOL +$119.45 predates the cy78
fix). With --backfill it writes the missing banks through the normal `append_partial_bank` path
(idempotent by cycle) — never a hand-edit of state.

    uv run python scripts/reconcile_cli.py                 # report only
    uv run python scripts/reconcile_cli.py --backfill      # also patch unrecorded banks to journal
"""
import argparse
import glob
import json
import os
import re

from futures_fund.journal import append_partial_bank, read_all_decisions
from futures_fund.reconcile import (
    open_position_banks,
    reconcile_balance,
    unrecorded_banks,
)


def report_reduce_events(state_dir: str) -> list[dict]:
    """Every reduce/scale-out bank recorded in a cycle report.json action log."""
    events: list[dict] = []
    for f in glob.glob(os.path.join(state_dir, "cycle", "*", "report.json")):
        m = re.search(r"cycle[\\/](\d+)[\\/]", f)
        if not m:
            continue
        cycle = int(m.group(1))
        try:
            rep = json.load(open(f))
        except (OSError, ValueError):
            continue
        ts = rep.get("candle") or rep.get("ran_at")
        for a in rep.get("actions", []) or []:
            if isinstance(a, dict) and "reduce" in a and not a.get("full", False):
                events.append({
                    "symbol": a.get("reduce"), "cycle": cycle, "pnl": a.get("pnl"),
                    "fraction": a.get("fraction"), "ts": ts,
                })
    return events


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", default="state")
    ap.add_argument("--memory", default="memory")
    ap.add_argument("--seed", type=float, default=10000.0)
    ap.add_argument("--tolerance", type=float, default=1.0,
                    help="warn if the residual left UNEXPLAINED after detected banks exceeds this")
    ap.add_argument("--backfill", action="store_true",
                    help="append unrecorded report banks to the journal via append_partial_bank")
    args = ap.parse_args()

    decisions = read_all_decisions(args.memory)
    balance = json.load(open(os.path.join(args.state, "account.json")))["balance"]
    # Open positions' entry fees were debited from balance at open but are absent from the
    # closed-trade journal — fold them in so a book that merely holds positions reconciles clean.
    try:
        open_positions = json.load(open(os.path.join(args.state, "positions.json")))
    except (OSError, ValueError):
        open_positions = []
    events = report_reduce_events(args.state)
    # A still-open reduced runner banked scale-out PnL to balance that is not yet in the closed
    # journal — fold it back so a book holding a runner reconciles clean (migrates to
    # scale_out_banks on the runner's final close).
    open_banks = open_position_banks(open_positions, events)
    missing = unrecorded_banks(decisions, events)

    if args.backfill and missing:
        for b in missing:
            append_partial_bank(args.memory, b["decision_id"], {
                "pnl": b["pnl"], "fraction": b["fraction"], "cycle": b["cycle"],
                "ts": b["ts"], "reason": "backfill: report-confirmed scale-out absent from journal",
            })
        # re-read so the reconciliation reflects the backfill
        decisions = read_all_decisions(args.memory)
        missing = unrecorded_banks(decisions, events)

    rec = reconcile_balance(decisions, balance, seed=args.seed, open_positions=open_positions,
                            open_position_banks=open_banks, report_events=events)
    detected = sum((b.get("pnl") or 0.0) for b in missing)
    unexplained = rec["residual"] - detected

    out = {
        **rec,
        "report_reduce_events": len(events),
        "unrecorded_banks": missing,
        "detected_missing_pnl": detected,
        "unexplained_residual": unexplained,
        "reconciled": abs(unexplained) <= args.tolerance,
        "backfilled": bool(args.backfill),
    }
    print(json.dumps(out, indent=2, default=str))
    if not out["reconciled"]:
        print(f"WARNING: ${unexplained:+.2f} of balance is NOT explained by the journal "
              f"(closes + recorded banks + entry fees) — investigate.")


if __name__ == "__main__":
    main()

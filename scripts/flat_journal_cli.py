"""Record this cycle's FLAT / declined-setup verdicts so the desk can later learn whether
standing aside helped or cost it (the data source for ENABLING lessons).

The orchestrator writes state/cycle/N/flat_verdicts.json — a list of objects:
  {"symbol":"XLMUSDT","regime":"high_vol_trend","rating":"flat","reason":"...",
   "edge_aligned":true,"favored_side":"long","mark":0.2509}
edge_aligned=true ONLY when the passed-on setup matched the desk's proven edge (a crowded-short
squeeze-long: L/S<~0.85 + negative funding, or an analyst/RM-flagged edge setup). favored_side is
the direction the bull/analysts leaned. mark is the brief's current mark_price.

    uv run python scripts/flat_journal_cli.py --cycle N
"""
from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from futures_fund.flat_journal import append_flat_decision


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cycle", type=int, required=True)
    args = ap.parse_args()
    p = Path("state") / "cycle" / str(args.cycle) / "flat_verdicts.json"
    if not p.exists():
        print(json.dumps({"recorded": 0, "note": "no flat_verdicts.json"}))
        return
    verdicts = json.loads(p.read_text())
    now = datetime.now(UTC)
    ids = []
    for v in verdicts:
        ids.append(append_flat_decision("memory", {**v, "cycle": args.cycle}, ts=now))
    print(json.dumps({"recorded": len(ids), "edge_aligned":
                      sum(1 for v in verdicts if v.get("edge_aligned"))}))


if __name__ == "__main__":
    main()

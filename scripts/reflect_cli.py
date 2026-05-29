"""Reflection CLI: emit the winners/losers payload for the Reflector subagent.

    uv run python scripts/reflect_cli.py --cycle N
"""
from __future__ import annotations

import argparse
import json

from futures_fund.cycle_io import save_output
from futures_fund.orchestration import reflect_step


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cycle", type=int, required=True)
    args = ap.parse_args()
    payload = reflect_step("memory")
    save_output("state", args.cycle, "reflection_input", payload)
    print(json.dumps(payload, indent=2, default=str))


if __name__ == "__main__":
    main()

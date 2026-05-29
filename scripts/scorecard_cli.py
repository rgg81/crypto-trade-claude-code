"""Print the desk scorecard (the stats digest injected into every agent prompt).

    uv run python scripts/scorecard_cli.py
"""
from __future__ import annotations

import json

from futures_fund.scorecard import build_scorecard


def main() -> None:
    sc = build_scorecard("state", "memory", monthly_target=0.05)
    print(json.dumps(sc, indent=2, default=str))


if __name__ == "__main__":
    main()

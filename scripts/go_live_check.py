"""Pre-flight readiness check before enabling live trading. Prints the graduation verdict and
the live-readiness gate. Does NOT place any orders.

    uv run python scripts/go_live_check.py
"""
from __future__ import annotations

import json

from futures_fund.config import load_settings
from futures_fund.live_gate import live_allowed
from futures_fund.scorecard import build_scorecard


def main() -> None:
    settings = load_settings()
    sc = build_scorecard("state", "memory", monthly_target=0.05)
    allowed = live_allowed(settings, sc)
    out = {
        "live_flag": getattr(settings, "live", False),
        "graduation": sc["graduation"],
        "equity": sc["equity"],
        "live_allowed": allowed,
        "verdict": (
            "READY — live trading permitted" if allowed
            else "NOT READY — stays in paper mode (need live=true AND a graduated verdict)"
        ),
    }
    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    main()

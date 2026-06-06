"""Standing self-audit CLI (Pillar 4 — AUDIT).

    uv run python scripts/self_audit_cli.py

Runs the desk's critical cross-module invariant panel (anti-martingale pacing, gate RR floor,
anti-hallucination, long/short symmetry, regime-routed playbook) and prints PASS/FAIL. Exits 0 when
all invariants hold, 1 otherwise. Fast, deterministic — a cheap standing check to run any cycle
alongside (not instead of) the full `uv run pytest` regression suite.
"""
from __future__ import annotations

from futures_fund.self_audit import run_self_audit


def main() -> None:
    res = run_self_audit()
    for c in res["checks"]:
        mark = "PASS" if c["ok"] else "FAIL"
        line = f"[{mark}] {c['name']}"
        if not c["ok"] and c["detail"]:
            line += f" — {c['detail']}"
        print(line)
    print(f"\nSELF-AUDIT: {'OK' if res['ok'] else 'FAILED'} "
          f"({sum(c['ok'] for c in res['checks'])}/{len(res['checks'])} invariants hold)")
    raise SystemExit(0 if res["ok"] else 1)


if __name__ == "__main__":
    main()

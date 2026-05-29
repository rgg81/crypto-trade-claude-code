from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path


def _path(state_dir) -> Path:
    return Path(state_dir) / "equity-history.jsonl"


def record_equity(state_dir, ts: datetime, equity: float, cycle: int) -> None:
    """Append the desk's total equity at the end of a cycle (the return series' source)."""
    p = _path(state_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as f:
        f.write(json.dumps({"ts": ts.isoformat(), "equity": float(equity), "cycle": cycle}) + "\n")


def equity_series(state_dir) -> list[tuple[str, float]]:
    p = _path(state_dir)
    if not p.exists():
        return []
    out = []
    for line in p.read_text().splitlines():
        if line.strip():
            r = json.loads(line)
            out.append((r["ts"], float(r["equity"])))
    return out


def returns_series(state_dir) -> list[float]:
    eq = [e for _, e in equity_series(state_dir)]
    return [(eq[i] / eq[i - 1] - 1.0) for i in range(1, len(eq)) if eq[i - 1] > 0]

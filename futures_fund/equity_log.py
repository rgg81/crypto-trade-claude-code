from __future__ import annotations

import json
from datetime import datetime, timedelta
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


def period_return(state_dir, now: datetime, days: float) -> float:
    """Return over the trailing `days`: latest equity vs the last equity at/before now-days
    (or the earliest on record if none is that old). 0.0 with < 2 points. Feeds the A1
    circuit breakers (daily/weekly/monthly)."""
    series = [(datetime.fromisoformat(ts), eq) for ts, eq in equity_series(state_dir)]
    if len(series) < 2:
        return 0.0
    cutoff = now - timedelta(days=days)
    older = [eq for ts, eq in series if ts <= cutoff]
    base = older[-1] if older else series[0][1]
    last = series[-1][1]
    return (last / base - 1.0) if base > 0 else 0.0

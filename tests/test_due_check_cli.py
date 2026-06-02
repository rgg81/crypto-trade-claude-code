"""Stdout/exit-code contract for scripts/due_check.py — the cron prompt branches on line 1."""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts" / "due_check.py"


def _current_candle_iso() -> str:
    now = datetime.now(UTC)
    return now.replace(hour=(now.hour // 4) * 4, minute=0, second=0, microsecond=0).isoformat()


def _run(state_dir: Path) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(CLI), str(state_dir)],
                          capture_output=True, text=True, cwd=ROOT)


def _report(state_dir: Path, n: int, candle: str, ran_at: str) -> None:
    d = state_dir / "cycle" / str(n)
    d.mkdir(parents=True, exist_ok=True)
    (d / "report.json").write_text(json.dumps({"cycle": n, "candle": candle, "ran_at": ran_at}))


def test_cli_cold_start_prints_due_fresh_exit0(tmp_path):
    r = _run(tmp_path)
    assert r.returncode == 0
    assert r.stdout.splitlines()[0].startswith("DUE FRESH ")


def test_cli_served_candle_prints_skip_exit0(tmp_path):
    # serving the CURRENT 4h candle guarantees SKIP regardless of when the test runs
    candle = _current_candle_iso()
    _report(tmp_path, 7, candle=candle, ran_at=candle)
    r = _run(tmp_path)
    assert r.returncode == 0
    assert r.stdout.splitlines()[0].startswith("SKIP:")


def test_cli_line1_is_machine_parseable(tmp_path):
    r = _run(tmp_path)
    first = r.stdout.splitlines()[0]
    # line 1 starts with exactly one of the three tokens the cron prompt keys on
    assert first.startswith(("DUE FRESH ", "DUE RETRY ", "SKIP:"))

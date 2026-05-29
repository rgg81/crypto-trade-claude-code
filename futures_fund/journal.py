from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from futures_fund.models import Direction


class Decision(BaseModel):
    """Two-phase decision record. Phase-1 fields written at decision time; Phase-2 (outcome)
    fields patched on close. extra='allow' lets Phase-B agents attach richer context."""

    model_config = ConfigDict(extra="allow")

    id: str
    ts: datetime
    cycle: int
    symbol: str
    direction: Direction
    entry: float
    stop: float
    # Phase-1 optional context
    take_profit: list[float] = Field(default_factory=list)
    size: float | None = None
    leverage: float | None = None
    r_multiple: float | None = None
    funding_at_entry: float | None = None
    regime: str | None = None
    confidence: float | None = None
    rationale: str | None = None
    dominant_signal: str | None = None
    contributing_agents: list[str] = Field(default_factory=list)
    retrieved_memory_ids: list[str] = Field(default_factory=list)
    # Phase-2 outcome (None until closed)
    exit_ts: datetime | None = None
    realized_pnl: float | None = None
    fees: float | None = None
    funding_paid: float | None = None
    slippage: float | None = None
    prediction_correct: bool | None = None
    low_level_lesson: str | None = None
    high_level_lesson: str | None = None
    importance: int | None = None


def _episodic_dir(memory_dir) -> Path:
    return Path(memory_dir) / "episodic"


def journal_file(memory_dir, ts: datetime) -> Path:
    return _episodic_dir(memory_dir) / f"journal-{ts:%Y-%m}.jsonl"


def append_decision(memory_dir, fields: dict) -> str:
    """Validate and append a Phase-1 decision; returns its id (generated if absent)."""
    data = dict(fields)
    data.setdefault("id", uuid.uuid4().hex)
    decision = Decision.model_validate(data)
    f = journal_file(memory_dir, decision.ts)
    f.parent.mkdir(parents=True, exist_ok=True)
    with f.open("a") as fh:
        fh.write(decision.model_dump_json() + "\n")
    return decision.id


def _all_files(memory_dir) -> list[Path]:
    d = _episodic_dir(memory_dir)
    return sorted(d.glob("journal-*.jsonl")) if d.exists() else []


def read_all_decisions(memory_dir) -> list[dict]:
    out: list[dict] = []
    for f in _all_files(memory_dir):
        for line in f.read_text().splitlines():
            if line.strip():
                out.append(json.loads(line))
    return out


def read_open_decisions(memory_dir) -> list[dict]:
    """Decisions without a realized outcome yet (Phase-2 not filled)."""
    return [r for r in read_all_decisions(memory_dir) if r.get("realized_pnl") is None]


def patch_outcome(memory_dir, decision_id: str, outcome: dict) -> bool:
    """Merge Phase-2 outcome fields into the decision with `decision_id`. Rewrites the
    containing monthly file. Returns False if the id is not found."""
    for f in _all_files(memory_dir):
        records = [json.loads(line) for line in f.read_text().splitlines() if line.strip()]
        hit = False
        for r in records:
            if r.get("id") == decision_id:
                # validate the merged record so outcome types are coerced (e.g. datetimes)
                merged = Decision.model_validate({**r, **outcome})
                r.clear()
                r.update(json.loads(merged.model_dump_json()))
                hit = True
        if hit:
            f.write_text("".join(json.dumps(r) + "\n" for r in records))
            return True
    return False

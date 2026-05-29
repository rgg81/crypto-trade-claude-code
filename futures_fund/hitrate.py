from __future__ import annotations

import json
from pathlib import Path

_MAX_HISTORY = 200  # cap stored outcomes per agent


def _scores_path(memory_dir) -> Path:
    return Path(memory_dir) / "hitrate" / "agent_scores.json"


def _load(memory_dir) -> dict[str, list[bool]]:
    p = _scores_path(memory_dir)
    return json.loads(p.read_text()) if p.exists() else {}


def _save(memory_dir, data: dict[str, list[bool]]) -> None:
    p = _scores_path(memory_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2))


def record_outcome(memory_dir, agent: str, correct: bool) -> None:
    data = _load(memory_dir)
    history = data.get(agent, [])
    history.append(bool(correct))
    data[agent] = history[-_MAX_HISTORY:]
    _save(memory_dir, data)


def hit_rate(memory_dir, agent: str, window: int = 30) -> float:
    """Rolling hit rate over the last `window` outcomes. Defaults to 0.5 with no history."""
    history = _load(memory_dir).get(agent, [])
    if not history:
        return 0.5
    recent = history[-window:]
    return sum(recent) / len(recent)

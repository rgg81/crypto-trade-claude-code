from __future__ import annotations

from pathlib import Path

_SEED = {
    "beliefs": "# Beliefs\n\nEvolving per-symbol / per-regime beliefs. Each entry should cite the\n"
               "journal decision ids that support it.\n",
    "lessons": (
        "# Lessons\n\nCANDIDATE and VALIDATED lessons with provenance. VALIDATED lessons become\n"
        "hard vetoes; demote aggressively when a regime shifts.\n"
    ),
    "playbook": "# Playbook\n\nThe team's standing trading rules (procedural memory).\n",
    "repair_journal": "# Repair Journal\n\nOrchestrator code-fix audit trail "
                      "(symptom -> root cause -> fix -> verification).\n",
}


def memory_paths(memory_dir) -> dict[str, Path]:
    root = Path(memory_dir)
    return {
        "episodic": root / "episodic",
        "semantic": root / "semantic",
        "procedural": root / "procedural",
        "lessons": root / "lessons" / "lessons.md",
        "beliefs": root / "semantic" / "beliefs.md",
        "playbook": root / "procedural" / "playbook.md",
        "hitrate": root / "hitrate",
        "repair_journal": root / "repair-journal.md",
    }


def ensure_memory_layout(memory_dir) -> dict[str, Path]:
    """Create the memory directory tree and seed the markdown files if absent.
    Idempotent: never overwrites existing files."""
    paths = memory_paths(memory_dir)
    for key in ("episodic", "semantic", "procedural", "hitrate"):
        paths[key].mkdir(parents=True, exist_ok=True)
    paths["lessons"].parent.mkdir(parents=True, exist_ok=True)
    for key, seed in _SEED.items():
        if not paths[key].exists():
            paths[key].write_text(seed)
    return paths

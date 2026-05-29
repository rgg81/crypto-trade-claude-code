# Futures-Fund Phase B3 — Self-Healing & Learning Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Close the self-improving loop: (1) self-healing **repair rails** (structured error log + repair journal + a protected-paths guardrail so a code fix can never weaken a risk/exec limit), (2) wire **lesson retrieval** into the cycle (the team learns from past decisions), and (3) **gated lesson promotion** (CANDIDATE→VALIDATED→retired mechanics, with VALIDATED lessons as the hard-veto set).

**Architecture:** Pure-ish Python rails the orchestrator calls; the diagnose→fix→commit loop itself is performed by the orchestrator (Claude) at runtime per `SKILL.md`, which B3 hardens. Lesson promotion is count-based here (the statistical/DSR gate that *also* guards promotion arrives in Phase C). No network/LLM in tests.

**Tech Stack:** Python 3.11 / uv, pydantic v2, pytest, ruff.

**Reference:** spec §5 (self-healing), §6 (memory promotion/demotion). Builds on B1 `lessons.py` (Lesson, append/read/retrieve), B2 `orchestration.py`, `cycle_io`, `SKILL.md`.

---

## File Structure

```
futures_fund/
  repair.py        # log_error, record_repair, PROTECTED_PATHS/is_protected
  lessons.py       # (extend) update_lesson, confirm_lesson, demote_lesson, retire_lesson, validated_lessons
  orchestration.py # (extend) lessons_step
scripts/
  retrieve_lessons_cli.py · promote_lesson_cli.py
SKILL.md           # (extend) self-healing + lesson retrieval/promotion steps
tests/
  test_repair.py · test_lesson_promotion.py · test_lessons_step.py
```

---

## Task 1: Repair rails (`repair.py`)

**Files:** create `futures_fund/repair.py`, `tests/test_repair.py`.

- [ ] **Step 1: Write the failing test** — `tests/test_repair.py`:

```python
import json
from datetime import datetime, timezone

from futures_fund.repair import is_protected, log_error, record_repair

UTC = timezone.utc


def test_is_protected_flags_risk_and_exec_modules():
    assert is_protected("futures_fund/risk_gate.py") is True
    assert is_protected("futures_fund/executor.py") is True
    assert is_protected("cycle.py") is True
    assert is_protected("futures_fund/brief.py") is False
    assert is_protected("futures_fund/news.py") is False


def test_log_error_appends_jsonl(tmp_path):
    log_error(tmp_path, phase="execute", command="gate_execute_cli", error="boom",
              traceback="Traceback...", ts=datetime(2026, 5, 1, tzinfo=UTC))
    log_error(tmp_path, phase="screen", command="screen_cli", error="bad json",
              ts=datetime(2026, 5, 1, 1, tzinfo=UTC))
    lines = [json.loads(x) for x in (tmp_path / "error-log.jsonl").read_text().splitlines() if x.strip()]
    assert len(lines) == 2
    assert lines[0]["phase"] == "execute" and lines[0]["error"] == "boom"


def test_record_repair_appends_structured_entry(tmp_path):
    record_repair(tmp_path, symptom="screen crashed on dict input",
                  root_cause="analyst reports saved dict-wrapped",
                  fix="screen_step tolerates dict", verification="186 tests green",
                  ts=datetime(2026, 5, 1, tzinfo=UTC))
    md = (tmp_path / "repair-journal.md").read_text()
    assert "Symptom" in md and "Root cause" in md and "Verification" in md
    assert "screen crashed on dict input" in md
```

- [ ] **Step 2: Run** `uv run pytest tests/test_repair.py -v` — expect FAIL.

- [ ] **Step 3: Implement** `futures_fund/repair.py`:

```python
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

# Safety-critical modules: a self-healing "fix" may NEVER weaken a risk or execution limit
# here (spec §5). The orchestrator must keep the full test suite green before committing a
# change to any of these, and HALT rather than bypass a limit it cannot fix safely.
PROTECTED_PATHS = ("risk_gate", "executor", "exits", "consolidation", "policy",
                   "liquidation", "sizing", "cycle")


def is_protected(path: str) -> bool:
    """True if `path` is one of the risk/execution-critical modules."""
    return Path(path).stem in PROTECTED_PATHS


def log_error(state_dir, *, phase: str, command: str, error: str,
              ts: datetime, traceback: str = "") -> Path:
    """Append a structured error record to state/error-log.jsonl (no silent failures)."""
    p = Path(state_dir) / "error-log.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    rec = {"ts": ts.isoformat(), "phase": phase, "command": command,
           "error": error, "traceback": traceback[:2000]}
    with p.open("a") as f:
        f.write(json.dumps(rec) + "\n")
    return p


def record_repair(memory_dir, *, symptom: str, root_cause: str, fix: str,
                  verification: str, ts: datetime) -> Path:
    """Append an auditable repair entry to memory/repair-journal.md (committed)."""
    p = Path(memory_dir) / "repair-journal.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as f:
        f.write(f"\n## {ts:%Y-%m-%d %H:%M} repair\n"
                f"- **Symptom:** {symptom}\n"
                f"- **Root cause:** {root_cause}\n"
                f"- **Fix:** {fix}\n"
                f"- **Verification:** {verification}\n")
    return p
```

- [ ] **Step 4: Run** `uv run pytest tests/test_repair.py -v` — expect PASS (3 passed). Then `uv run ruff check futures_fund/repair.py tests/test_repair.py`.

- [ ] **Step 5: Commit**

```bash
git add futures_fund/repair.py tests/test_repair.py
git commit -m "feat: self-healing repair rails (error log, repair journal, protected paths)"
```

---

## Task 2: Gated lesson promotion (`lessons.py` extensions)

**Files:** modify `futures_fund/lessons.py`; create `tests/test_lesson_promotion.py`.

- [ ] **Step 1: Write the failing test** — `tests/test_lesson_promotion.py`:

```python
from datetime import datetime, timezone

from futures_fund.lessons import (
    append_lesson,
    confirm_lesson,
    demote_lesson,
    read_lessons,
    retire_lesson,
    update_lesson,
    validated_lessons,
)

UTC = timezone.utc


def _add(tmp_path, **over):
    base = dict(text="cut leverage in chop", regime="high_vol_range", tags=["vol"], importance=6)
    base.update(over)
    return append_lesson(tmp_path, base, ts=datetime(2026, 5, 1, tzinfo=UTC))


def test_update_lesson_rewrites_field(tmp_path):
    lid = _add(tmp_path)
    assert update_lesson(tmp_path, lid, importance=9) is True
    assert next(lz for lz in read_lessons(tmp_path) if lz.id == lid).importance == 9
    assert update_lesson(tmp_path, "missing", importance=1) is False


def test_confirm_promotes_candidate_at_threshold(tmp_path):
    lid = _add(tmp_path)
    for _ in range(4):
        confirm_lesson(tmp_path, lid, promote_threshold=5)
    assert next(lz for lz in read_lessons(tmp_path) if lz.id == lid).state == "candidate"
    confirm_lesson(tmp_path, lid, promote_threshold=5)  # 5th confirmation
    lz = next(lz for lz in read_lessons(tmp_path) if lz.id == lid)
    assert lz.state == "validated" and lz.confirmations == 5


def test_validated_lessons_are_the_veto_set(tmp_path):
    a = _add(tmp_path, text="A")
    _add(tmp_path, text="B")  # stays candidate
    for _ in range(5):
        confirm_lesson(tmp_path, a, promote_threshold=5)
    vals = validated_lessons(tmp_path)
    assert [lz.text for lz in vals] == ["A"]


def test_demote_steps_down_then_retires(tmp_path):
    lid = _add(tmp_path)
    for _ in range(5):
        confirm_lesson(tmp_path, lid, promote_threshold=5)  # -> validated
    assert demote_lesson(tmp_path, lid) is True  # validated -> candidate
    assert next(lz for lz in read_lessons(tmp_path) if lz.id == lid).state == "candidate"
    assert demote_lesson(tmp_path, lid) is True  # candidate -> retired
    assert next(lz for lz in read_lessons(tmp_path) if lz.id == lid).state == "retired"


def test_retire_lesson(tmp_path):
    lid = _add(tmp_path)
    assert retire_lesson(tmp_path, lid) is True
    assert next(lz for lz in read_lessons(tmp_path) if lz.id == lid).state == "retired"
```

- [ ] **Step 2: Run** `uv run pytest tests/test_lesson_promotion.py -v` — expect FAIL.

- [ ] **Step 3: Implement** — append to `futures_fund/lessons.py`:

```python
def _write_all(memory_dir, lessons: list[Lesson]) -> None:
    p = _store(memory_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("".join(lz.model_dump_json() + "\n" for lz in lessons))


def update_lesson(memory_dir, lesson_id: str, **fields) -> bool:
    """Merge `fields` into the lesson with `lesson_id`; rewrites the store. False if not found."""
    lessons = read_lessons(memory_dir)
    hit = False
    for i, lz in enumerate(lessons):
        if lz.id == lesson_id:
            lessons[i] = lz.model_copy(update=fields)
            hit = True
    if hit:
        _write_all(memory_dir, lessons)
    return hit


def confirm_lesson(memory_dir, lesson_id: str, *, promote_threshold: int = 5) -> bool:
    """Increment a lesson's confirmation count; promote CANDIDATE -> VALIDATED at the threshold.
    (Count-based here; Phase C gates promotion additionally on statistical support — spec §6.)"""
    lessons = read_lessons(memory_dir)
    hit = False
    for i, lz in enumerate(lessons):
        if lz.id == lesson_id:
            c = lz.confirmations + 1
            state = "validated" if (lz.state == "candidate" and c >= promote_threshold) else lz.state
            lessons[i] = lz.model_copy(update={"confirmations": c, "state": state})
            hit = True
    if hit:
        _write_all(memory_dir, lessons)
    return hit


def demote_lesson(memory_dir, lesson_id: str) -> bool:
    """Step a lesson down: VALIDATED -> CANDIDATE, CANDIDATE/RETIRED -> RETIRED.
    Used to aggressively age out stale or regime-mismatched rules (spec §6)."""
    lessons = read_lessons(memory_dir)
    hit = False
    for i, lz in enumerate(lessons):
        if lz.id == lesson_id:
            new = "candidate" if lz.state == "validated" else "retired"
            lessons[i] = lz.model_copy(update={"state": new})
            hit = True
    if hit:
        _write_all(memory_dir, lessons)
    return hit


def retire_lesson(memory_dir, lesson_id: str) -> bool:
    return update_lesson(memory_dir, lesson_id, state="retired")


def validated_lessons(memory_dir) -> list[Lesson]:
    """The VALIDATED lessons — these act as hard vetoes / standing rules for the team."""
    return [lz for lz in read_lessons(memory_dir) if lz.state == "validated"]
```

- [ ] **Step 4: Run** `uv run pytest tests/test_lesson_promotion.py -v` — expect PASS (5 passed). Then `uv run ruff check futures_fund/lessons.py tests/test_lesson_promotion.py`.

- [ ] **Step 5: Commit**

```bash
git add futures_fund/lessons.py tests/test_lesson_promotion.py
git commit -m "feat: gated lesson promotion (confirm/demote/retire, validated veto set)"
```

---

## Task 3: Lesson-retrieval step + CLIs

**Files:** modify `futures_fund/orchestration.py`; create `scripts/retrieve_lessons_cli.py`, `scripts/promote_lesson_cli.py`, `tests/test_lessons_step.py`.

- [ ] **Step 1: Write the failing test** — `tests/test_lessons_step.py`:

```python
from datetime import datetime, timezone

from futures_fund.lessons import append_lesson
from futures_fund.memory_layout import ensure_memory_layout
from futures_fund.orchestration import lessons_step

UTC = timezone.utc


def test_lessons_step_returns_regime_filtered_dicts(tmp_path):
    ensure_memory_layout(tmp_path)
    append_lesson(tmp_path, {"text": "trend lesson", "regime": "high_vol_trend", "tags": ["trend"]},
                  ts=datetime(2026, 5, 1, tzinfo=UTC))
    append_lesson(tmp_path, {"text": "range lesson", "regime": "low_vol_range", "tags": ["mr"]},
                  ts=datetime(2026, 5, 1, tzinfo=UTC))
    got = lessons_step(tmp_path, now=datetime(2026, 5, 2, tzinfo=UTC),
                       regime="high_vol_trend", tags=["trend"], k=5)
    assert isinstance(got, list) and all(isinstance(x, dict) for x in got)
    texts = [x["text"] for x in got]
    assert "trend lesson" in texts and "range lesson" not in texts


def test_lessons_step_empty_when_none(tmp_path):
    ensure_memory_layout(tmp_path)
    assert lessons_step(tmp_path, now=datetime(2026, 5, 2, tzinfo=UTC),
                        regime="x", tags=["y"], k=5) == []
```

- [ ] **Step 2: Run** `uv run pytest tests/test_lessons_step.py -v` — expect FAIL.

- [ ] **Step 3: Implement** — append to `futures_fund/orchestration.py`:

```python
def lessons_step(memory_dir, now, regime: str | None, tags: list[str], k: int = 5) -> list[dict]:
    """Retrieve the top-K regime-relevant lessons (as JSON dicts) for injection into the
    debate/trader subagent prompts, so the team learns from past decisions (spec §6)."""
    from futures_fund.lessons import retrieve_lessons
    return [lz.model_dump(mode="json") for lz in retrieve_lessons(memory_dir, now, regime, tags, k)]
```

- [ ] **Step 4: Create the CLIs.** `scripts/retrieve_lessons_cli.py`:

```python
"""Retrieve regime-relevant lessons for the debate/trader prompts.

    uv run python scripts/retrieve_lessons_cli.py --cycle N --regime high_vol_trend --tags trend,funding --k 5
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

from futures_fund.cycle_io import save_output
from futures_fund.orchestration import lessons_step


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cycle", type=int, required=True)
    ap.add_argument("--regime", default=None)
    ap.add_argument("--tags", default="")
    ap.add_argument("--k", type=int, default=5)
    args = ap.parse_args()
    tags = [t for t in args.tags.split(",") if t]
    lessons = lessons_step("memory", datetime.now(timezone.utc), args.regime, tags, args.k)
    save_output("state", args.cycle, "lessons", {"lessons": lessons})
    print(json.dumps({"lessons": lessons}, indent=2, default=str))


if __name__ == "__main__":
    main()
```

`scripts/promote_lesson_cli.py`:

```python
"""Apply a Reflector-decided lesson state change.

    uv run python scripts/promote_lesson_cli.py --id <lesson_id> --action confirm|demote|retire
"""
from __future__ import annotations

import argparse

from futures_fund.lessons import confirm_lesson, demote_lesson, retire_lesson


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", required=True)
    ap.add_argument("--action", choices=["confirm", "demote", "retire"], required=True)
    args = ap.parse_args()
    fn = {"confirm": confirm_lesson, "demote": demote_lesson, "retire": retire_lesson}[args.action]
    ok = fn("memory", args.id)
    print(f"{args.action} {args.id}: {'ok' if ok else 'not found'}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 5: Run** `uv run pytest tests/test_lessons_step.py -v` — expect PASS (2 passed). Then `uv run ruff check futures_fund/orchestration.py scripts/retrieve_lessons_cli.py scripts/promote_lesson_cli.py tests/test_lessons_step.py`.

- [ ] **Step 6: Commit**

```bash
git add futures_fund/orchestration.py scripts/retrieve_lessons_cli.py scripts/promote_lesson_cli.py tests/test_lessons_step.py
git commit -m "feat: lesson-retrieval step + retrieve/promote CLIs (wire learning into the cycle)"
```

---

## Task 4: Harden `SKILL.md` (self-healing + learning steps) + structural test

**Files:** modify `SKILL.md`; create `tests/test_skill_selfheal.py`.

- [ ] **Step 1: Edit `SKILL.md`.** (a) In Phase 5 (debate), before dispatching Bull/Bear, insert a lesson-retrieval step:

```markdown
   Before the debate, run `uv run python scripts/retrieve_lessons_cli.py --cycle N --regime <symbol's quadrant from the brief> --tags <setup tags> --k 5` and inject the returned `state/cycle/N/lessons.json` (top 3-7 VALIDATED/relevant lessons) into the Bull, Bear, and Trader prompts — the team must reason WITH its past lessons.
```

(b) In Phase 11 (reflect), after recording CANDIDATE lessons, add:

```markdown
   The Reflector may also confirm/demote/retire EXISTING lessons based on the closed trades: for each, run `uv run python scripts/promote_lesson_cli.py --id <lesson_id> --action confirm|demote|retire`. A lesson reaching the confirmation threshold becomes VALIDATED (a standing rule); stale or regime-mismatched VALIDATED lessons must be demoted aggressively so vetoes don't ossify.
```

(c) Replace the `## Self-healing` section with this hardened version:

```markdown
## Self-healing (spec §5)
If any `scripts/*` call errors:
1. Log it: append the error (phase, command, message, traceback) to `state/error-log.jsonl` (use `futures_fund.repair.log_error`).
2. Diagnose the ROOT cause with the systematic-debugging skill — never guess-patch.
3. Fix the code. **GUARDRAIL:** a fix to any protected module (`futures_fund.repair.is_protected` → risk_gate, executor, exits, consolidation, policy, liquidation, sizing, cycle) may NEVER weaken a risk limit, disable a circuit breaker, or bypass the execution safety path to make the error go away. The FULL test suite (`uv run pytest`) must pass before you commit any fix.
4. Verify (re-run the failed step + the suite), commit the fix on a branch, and append the repair (symptom → root cause → fix → verification) to `memory/repair-journal.md` via `futures_fund.repair.record_repair`.
5. Resume the cycle from the failed phase, or degrade safely (cap conviction / skip the affected symbol).
If you cannot fix it safely, set the HALT flag (`futures_fund.state.set_halt`) and surface for human review — bad trades are worse than a paused desk.
```

- [ ] **Step 2: Write the structural test** — `tests/test_skill_selfheal.py`:

```python
from pathlib import Path


def test_skill_documents_selfhealing_and_learning():
    t = Path("SKILL.md").read_text()
    assert "repair-journal.md" in t
    assert "retrieve_lessons_cli.py" in t
    assert "promote_lesson_cli.py" in t
    assert "GUARDRAIL" in t and "HALT" in t


def test_repair_journal_seeded():
    # memory_layout (A3a) seeds repair-journal.md; the self-heal docs reference it
    assert "repair-journal" in Path("SKILL.md").read_text()
```

- [ ] **Step 3: Run** `uv run pytest tests/test_skill_selfheal.py -v` — expect PASS (2 passed).

- [ ] **Step 4: Run the FULL suite + lint** `uv run pytest` then `uv run ruff check .`. Report the EXACT total (expected 186 + repair 3 + promotion 5 + lessons_step 2 + selfheal 2 = **198**).

- [ ] **Step 5: Commit**

```bash
git add SKILL.md tests/test_skill_selfheal.py
git commit -m "feat: harden SKILL.md self-healing guardrail + wire lesson retrieval/promotion"
```

---

## Self-Review (completed during planning)

**Spec coverage (§5 self-healing, §6 promotion):** structured error log + repair journal + protected-paths guardrail ✓ (T1); lesson promotion CANDIDATE→VALIDATED + demote/retire + the VALIDATED veto set ✓ (T2); lesson retrieval wired into the cycle ✓ (T3); SKILL.md self-healing loop + the GUARDRAIL that a fix may never weaken a risk/exec limit (else HALT) ✓ (T4). Deferred to C (correct): the statistical/DSR gate that *also* guards promotion (B3 is count-based); auto-confirmation from outcome attribution (the Reflector drives promotion via the CLI for now).

**Placeholder scan:** none — runnable code/tests + exact SKILL.md edits.

**Type consistency:** `confirm_lesson`/`demote_lesson`/`update_lesson`/`validated_lessons` operate on the B1 `Lesson` model (state/confirmations fields exist). `lessons_step` wraps B1 `retrieve_lessons`, returns JSON dicts. `repair.is_protected` lists the A1/A3b/B1 risk+exec modules. CLIs use `cycle_io.save_output` + the lessons functions.

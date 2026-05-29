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

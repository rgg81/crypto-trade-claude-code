from futures_fund.memory_layout import ensure_memory_layout, memory_paths


def test_ensure_creates_all_dirs_and_seed_files(tmp_path):
    paths = ensure_memory_layout(tmp_path)
    assert (tmp_path / "episodic").is_dir()
    assert (tmp_path / "hitrate").is_dir()
    assert paths["beliefs"].exists() and paths["beliefs"].name == "beliefs.md"
    assert paths["lessons"].exists() and paths["lessons"].name == "lessons.md"
    assert paths["playbook"].exists() and paths["playbook"].name == "playbook.md"
    assert paths["repair_journal"].exists() and paths["repair_journal"].name == "repair-journal.md"
    # seed files are non-empty (have a heading)
    assert paths["beliefs"].read_text().strip().startswith("#")


def test_ensure_is_idempotent_and_preserves_content(tmp_path):
    paths = ensure_memory_layout(tmp_path)
    paths["lessons"].write_text("# Lessons\n\n- VALIDATED: don't fight strong funding\n")
    ensure_memory_layout(tmp_path)  # second call must not clobber
    assert "don't fight strong funding" in paths["lessons"].read_text()


def test_memory_paths_returns_expected_keys(tmp_path):
    p = memory_paths(tmp_path)
    assert set(p) >= {
        "episodic", "semantic", "procedural", "lessons", "beliefs", "playbook", "hitrate"
    }

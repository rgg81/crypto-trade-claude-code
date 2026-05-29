from pathlib import Path

import pytest

ROLES = ["watcher", "technical", "derivatives", "news", "sentiment", "bull", "bear",
         "research_manager", "trader", "risk_manager", "portfolio_manager", "reflector"]


@pytest.mark.parametrize("role", ROLES)
def test_role_file_exists_and_has_sections(role):
    p = Path("agents") / f"{role}.md"
    assert p.exists(), f"missing role file: {p}"
    text = p.read_text()
    assert "## Mission" in text
    # analyst/decision agents must specify an Output contract; the two deterministic docs are exempt
    if role not in ("risk_manager", "portfolio_manager"):
        assert "## Output" in text, f"{role} missing Output section"


def test_mission_file_exists_and_is_the_charter():
    t = Path("MISSION.md").read_text()
    assert "OPERATION TEMPEST" in t and "5%" in t

import json
from datetime import UTC
from pathlib import Path

import pytest

from futures_fund.contracts import (
    AgentProposal,
    AnalystReport,
    ResearchPlan,
    WatcherOutput,
)
from futures_fund.lessons import Lesson

FIX = Path(__file__).parent / "fixtures" / "agent_examples"


def _load(name):
    return json.loads((FIX / name).read_text())


def test_watcher_example_conforms():
    WatcherOutput.model_validate(_load("watcher.json"))


@pytest.mark.parametrize(
    "name", ["technical.json", "derivatives.json", "news.json", "sentiment.json"]
)
def test_analyst_examples_conform(name):
    r = AnalystReport.model_validate(_load(name))
    assert r.stance in {"bullish", "bearish", "neutral"}


def test_research_plan_example_conforms():
    p = ResearchPlan.model_validate(_load("research_plan.json"))
    assert p.rating in {"strong_long", "long", "flat", "short", "strong_short"}
    assert p.falsifiable_prediction


def test_trader_example_conforms():
    ap = AgentProposal.model_validate(_load("trader.json"))
    assert ap.symbol == "BTCUSDT" and ap.direction == "long"


def test_reflector_example_lessons_conform():
    data = _load("reflector.json")
    from datetime import datetime
    for lz in data["lessons"]:
        Lesson.model_validate({**lz, "ts": datetime(2026, 5, 1, tzinfo=UTC)})

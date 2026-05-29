from datetime import UTC, datetime

from futures_fund.lessons import append_lesson
from futures_fund.memory_layout import ensure_memory_layout
from futures_fund.orchestration import lessons_step


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

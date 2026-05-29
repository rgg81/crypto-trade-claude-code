from datetime import UTC, datetime, timedelta

from futures_fund.lessons import (
    Lesson,
    append_lesson,
    read_lessons,
    retrieve_lessons,
    score_lesson,
)


def _lesson(**over):
    base = dict(text="don't fight strong funding", regime="high_vol_trend",
                symbol="BTCUSDT", tags=["funding", "trend"], importance=8)
    base.update(over)
    return base


def test_append_returns_id_and_read_roundtrip(tmp_path):
    lid = append_lesson(tmp_path, _lesson(), ts=datetime(2026, 5, 1, tzinfo=UTC))
    lessons = read_lessons(tmp_path)
    assert len(lessons) == 1 and lessons[0].id == lid
    assert lessons[0].state == "candidate" and lessons[0].importance == 8


def test_score_combines_recency_importance_relevance():
    now = datetime(2026, 5, 2, tzinfo=UTC)
    recent = Lesson(id="a", ts=now - timedelta(hours=1), text="x", importance=10,
                    tags=["funding"])
    old = Lesson(id="b", ts=now - timedelta(hours=500), text="y", importance=10,
                 tags=["funding"])
    # same importance & relevance; the recent one must score higher
    assert score_lesson(recent, now, ["funding"]) > score_lesson(old, now, ["funding"])
    # tag overlap raises relevance
    s_match = score_lesson(recent, now, ["funding"])
    s_nomatch = score_lesson(recent, now, ["macro"])
    assert s_match > s_nomatch


def test_retrieve_filters_by_regime_then_ranks_top_k(tmp_path):
    now = datetime(2026, 5, 2, tzinfo=UTC)
    append_lesson(tmp_path, _lesson(text="trend lesson", regime="high_vol_trend",
                                    tags=["trend"]), ts=now - timedelta(hours=2))
    append_lesson(tmp_path, _lesson(text="range lesson", regime="low_vol_range",
                                    tags=["meanrev"]), ts=now - timedelta(hours=2))
    append_lesson(tmp_path, _lesson(text="universal", regime=None, tags=["risk"]),
                  ts=now - timedelta(hours=2))
    got = retrieve_lessons(tmp_path, now=now, regime="high_vol_trend",
                           query_tags=["trend"], k=5)
    texts = [lz.text for lz in got]
    assert "trend lesson" in texts        # matching regime
    assert "universal" in texts           # regime=None applies everywhere
    assert "range lesson" not in texts    # wrong regime filtered out


def test_retrieve_respects_top_k(tmp_path):
    now = datetime(2026, 5, 2, tzinfo=UTC)
    for i in range(10):
        append_lesson(tmp_path, _lesson(text=f"l{i}", regime=None, tags=["risk"]),
                      ts=now - timedelta(hours=i + 1))
    assert len(retrieve_lessons(tmp_path, now=now, regime="x", query_tags=["risk"], k=3)) == 3

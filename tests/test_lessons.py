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


def test_retrieve_matches_engine_label_and_quadrant_and_any(tmp_path):
    # cy77/78 retrospective P0: 50 lessons are tagged with the ENGINE label ('risk_off') and 11 with
    # 'any', but SKILL passes the symbol QUADRANT ('high_vol_trend') as the query — so they were all
    # STRANDED. Retrieval must accept BOTH contexts (quadrant + engine label) and treat 'any' as
    # universal, so a risk_off edge lesson surfaces in a risk_off cycle regardless of quadrant.
    now = datetime(2026, 5, 2, tzinfo=UTC)
    append_lesson(tmp_path, _lesson(text="risk_off edge", regime="risk_off", tags=["flush"]),
                  ts=now - timedelta(hours=2))
    append_lesson(tmp_path, _lesson(text="quadrant lesson", regime="high_vol_trend", tags=["t"]),
                  ts=now - timedelta(hours=2))
    append_lesson(tmp_path, _lesson(text="any lesson", regime="any", tags=["x"]),
                  ts=now - timedelta(hours=2))
    append_lesson(tmp_path, _lesson(text="risk_on only", regime="risk_on", tags=["y"]),
                  ts=now - timedelta(hours=2))
    # query carries BOTH the engine label and the symbol quadrant
    got = retrieve_lessons(tmp_path, now=now, regime=["risk_off", "high_vol_trend"],
                           query_tags=["flush"], k=10)
    texts = [lz.text for lz in got]
    assert "risk_off edge" in texts        # engine-label match (was stranded before)
    assert "quadrant lesson" in texts      # quadrant match
    assert "any lesson" in texts           # 'any' is universal (was stranded before)
    assert "risk_on only" not in texts     # a non-matching desk regime is still excluded
    # a single-string regime still works (back-compat)
    single = retrieve_lessons(tmp_path, now=now, regime="risk_off", query_tags=["flush"], k=10)
    assert "risk_off edge" in [lz.text for lz in single]
    assert "quadrant lesson" not in [lz.text for lz in single]   # only the matched context


def test_retrieve_respects_top_k(tmp_path):
    now = datetime(2026, 5, 2, tzinfo=UTC)
    for i in range(10):
        append_lesson(tmp_path, _lesson(text=f"l{i}", regime=None, tags=["risk"]),
                      ts=now - timedelta(hours=i + 1))
    assert len(retrieve_lessons(tmp_path, now=now, regime="x", query_tags=["risk"], k=3)) == 3


# --- cy321: tag-vocabulary fragmentation silently zeroed retrieval relevance -----------------
# `score_lesson` compared raw tag STRINGS as sets, so relevance was exact-match-or-nothing. The
# corpus meanwhile grew THREE competing conventions that collide on the same concepts —
# 1008 hyphenated tags, 496 underscored, 544 single-word, including `adx-gate` / `adx_gate` /
# `ADX_gate` as three spellings of ONE tag. At cy321 the desk's only 2-for-2 predictive lesson
# (298c6d2f, tagged `trap-signature` / `vertical-move` / `catalyst-provenance`) scored ZERO
# relevance against a query naming exactly those concepts as `trap` / `vertical_move` /
# `catalyst`, so it never surfaced on the one cycle its own pattern was the central question.
# A validated lesson that cannot be retrieved when its pattern appears is functionally absent.

def test_tag_tokens_normalizes_separators_and_case():
    from futures_fund.lessons import tag_tokens
    assert tag_tokens(["vertical-move"]) == tag_tokens(["vertical_move"]) == {"vertical", "move"}
    assert tag_tokens(["ADX_gate"]) == tag_tokens(["adx-gate"]) == {"adx", "gate"}
    assert tag_tokens(["L/S-positioning"]) == {"l", "s", "positioning"}


def test_tag_tokens_is_failsafe_on_junk():
    from futures_fund.lessons import tag_tokens
    assert tag_tokens(None) == set()
    assert tag_tokens([]) == set()
    assert tag_tokens([None, 123, "", "  "]) == set()


def test_a_substring_concept_now_scores_nonzero_relevance():
    """`trap` must match `trap-signature` — the exact cy321 miss."""
    from datetime import UTC, datetime

    from futures_fund.lessons import Lesson, score_lesson
    now = datetime(2026, 8, 3, tzinfo=UTC)
    lz = Lesson(id="x", ts=now, text="t", regime=None, tags=["trap-signature", "vertical-move"],
                importance=7, provenance=[])
    hit = score_lesson(lz, now, ["trap", "vertical_move"])
    miss = score_lesson(lz, now, ["completely", "unrelated"])
    assert hit > miss, "concept-level overlap must outrank an unrelated query"


def test_identical_tag_sets_still_score_full_relevance():
    """Back-compat: an exact match must not regress."""
    from datetime import UTC, datetime

    from futures_fund.lessons import Lesson, score_lesson
    now = datetime(2026, 8, 3, tzinfo=UTC)
    lz = Lesson(id="x", ts=now, text="t", regime=None, tags=["geometry"], importance=5,
                provenance=[])
    same = score_lesson(lz, now, ["geometry"], w_rec=0.0, w_imp=0.0, w_rel=1.0)
    assert same == 1.0


def test_disjoint_tags_still_score_zero_relevance():
    from datetime import UTC, datetime

    from futures_fund.lessons import Lesson, score_lesson
    now = datetime(2026, 8, 3, tzinfo=UTC)
    lz = Lesson(id="x", ts=now, text="t", regime=None, tags=["funding"], importance=5,
                provenance=[])
    assert score_lesson(lz, now, ["geometry"], w_rec=0.0, w_imp=0.0, w_rel=1.0) == 0.0

"""Deterministic application of the Reflector's per-cycle lesson-confirmation scoring (#255).

The Reflector emits a `lesson_scoring` block — {confirm:[{id,why}], demote:[{id,why}]} — scoring
which RETRIEVED lessons a CLOSED trade's outcome validated or refuted. apply_lesson_scoring applies
it deterministically: confirms via the DSR-gated statistically_promote (a confirmation counts but a
candidate graduates to VALIDATED only at count>=5 AND DSR>=0.95), demotes via demote_lesson —
replacing the orchestrator hand-running promote_lesson_cli per id.
"""
from datetime import UTC, datetime

from futures_fund.lessons import append_lesson, apply_lesson_scoring, read_lessons


def _add(tmp_path, *, confirmations=0):
    return append_lesson(tmp_path, {"text": "x", "regime": "high_vol_trend", "tags": ["t"],
                                    "confirmations": confirmations},
                         ts=datetime(2026, 5, 1, tzinfo=UTC))


def _state(tmp_path, lid):
    return next(z for z in read_lessons(tmp_path) if z.id == lid)


def test_confirm_increments_but_stays_candidate_below_dsr_gate(tmp_path):
    lid = _add(tmp_path, confirmations=4)  # one more would hit threshold 5
    out = apply_lesson_scoring(tmp_path, {"confirm": [{"id": lid, "why": "outcome validated it"}]},
                               dsr_pvalue=0.16)
    lz = _state(tmp_path, lid)
    assert lz.confirmations == 5
    assert lz.state == "candidate"          # DSR 0.16 < 0.95 -> NO graduation
    assert out["confirmed"] == [lid]
    assert out["demoted"] == [] and out["not_found"] == []


def test_confirm_graduates_when_dsr_proven(tmp_path):
    lid = _add(tmp_path, confirmations=4)
    apply_lesson_scoring(tmp_path, {"confirm": [lid]}, dsr_pvalue=0.97)  # bare-string id accepted
    lz = _state(tmp_path, lid)
    assert lz.state == "validated" and lz.confirmations == 5


def test_demote_resets_via_demote_lesson(tmp_path):
    lid = _add(tmp_path, confirmations=3)
    out = apply_lesson_scoring(tmp_path, {"demote": [{"id": lid, "why": "x"}]}, dsr_pvalue=0.5)
    lz = _state(tmp_path, lid)
    assert lz.state == "retired"            # candidate -> retired on demote
    assert out["demoted"] == [lid]


def test_unknown_id_goes_to_not_found(tmp_path):
    out = apply_lesson_scoring(tmp_path, {"confirm": ["nope"], "demote": ["nada"]}, dsr_pvalue=0.5)
    assert set(out["not_found"]) == {"nope", "nada"}
    assert out["confirmed"] == [] and out["demoted"] == []


def test_empty_or_missing_scoring_is_noop(tmp_path):
    for scoring in (None, {}, {"confirm": [], "demote": []}):
        out = apply_lesson_scoring(tmp_path, scoring, dsr_pvalue=0.5)
        assert out == {"confirmed": [], "demoted": [], "not_found": [], "already_applied": []}
        # a no-op must stay a no-op with a cycle too — the idempotency ledger adds no side effects
        assert apply_lesson_scoring(tmp_path, scoring, dsr_pvalue=0.5, cycle_no=328) == out
        assert read_lessons(tmp_path) == []


def test_same_id_in_both_confirm_and_demote_demote_wins_is_safe(tmp_path):
    # defensive: a contradictory scoring must not crash; both are attempted, both report the id
    lid = _add(tmp_path, confirmations=1)
    out = apply_lesson_scoring(tmp_path, {"confirm": [lid], "demote": [lid]}, dsr_pvalue=0.5)
    assert lid in out["confirmed"] and lid in out["demoted"]


def test_confirm_is_idempotent_per_cycle(tmp_path):
    """cy328: `record_lessons` dedupes lessons BY TEXT, but lesson_scoring had no such guard — so
    re-running record_lessons_cli for the same cycle incremented the SAME confirmation again. It
    happened for real: an orchestrator re-run took migrating-pivot lesson 96a4a4b7 to
    confirmations=2 on a single genuine validation. A double-counted confirmation is not
    cosmetic — five of them
    graduate a CANDIDATE to VALIDATED, and validated lessons are never dropped by the retrieval
    quota, so a re-run can manufacture a standing rule the evidence never earned."""
    lid = _add(tmp_path, confirmations=0)
    scoring = {"confirm": [{"id": lid, "why": "outcome validated it"}]}
    first = apply_lesson_scoring(tmp_path, scoring, dsr_pvalue=0.16, cycle_no=328)
    second = apply_lesson_scoring(tmp_path, scoring, dsr_pvalue=0.16, cycle_no=328)
    assert _state(tmp_path, lid).confirmations == 1      # NOT 2
    assert first["confirmed"] == [lid]
    assert second["confirmed"] == []                     # already applied this cycle
    assert second["already_applied"] == [lid]


def test_a_later_cycle_can_confirm_the_same_lesson_again(tmp_path):
    """The guard is per-CYCLE, not permanent — a genuinely new validation must still count."""
    lid = _add(tmp_path, confirmations=0)
    scoring = {"confirm": [{"id": lid, "why": "validated again"}]}
    apply_lesson_scoring(tmp_path, scoring, dsr_pvalue=0.16, cycle_no=328)
    apply_lesson_scoring(tmp_path, scoring, dsr_pvalue=0.16, cycle_no=329)
    assert _state(tmp_path, lid).confirmations == 2
    assert _state(tmp_path, lid).confirmed_cycles == [328, 329]


def test_demote_is_also_idempotent_per_cycle(tmp_path):
    """A demote zeroes confirmations; re-running must not step VALIDATED->CANDIDATE->RETIRED."""
    lid = _add(tmp_path, confirmations=0)
    scoring = {"demote": [{"id": lid, "why": "refuted"}]}
    apply_lesson_scoring(tmp_path, scoring, dsr_pvalue=0.16, cycle_no=328)
    assert _state(tmp_path, lid).state == "retired"   # candidate -> retired
    apply_lesson_scoring(tmp_path, scoring, dsr_pvalue=0.16, cycle_no=328)
    assert _state(tmp_path, lid).state == "retired"      # unchanged, not stepped again


def test_scoring_without_a_cycle_number_still_applies(tmp_path):
    """Back-compat: callers that pass no cycle keep the old unguarded behaviour."""
    lid = _add(tmp_path, confirmations=0)
    apply_lesson_scoring(tmp_path, {"confirm": [lid]}, dsr_pvalue=0.16)
    assert _state(tmp_path, lid).confirmations == 1

from datetime import UTC, datetime

from futures_fund.journal import (
    Decision,
    append_decision,
    journal_file,
    patch_outcome,
    read_all_decisions,
    read_open_decisions,
)


def _decision(**over):
    base = dict(
        ts=datetime(2026, 5, 29, 12, tzinfo=UTC), cycle=1, symbol="BTCUSDT",
        direction="long", entry=100.0, stop=95.0, confidence=0.7,
        rationale="momentum breakout", dominant_signal="trend",
    )
    base.update(over)
    return base


def test_append_returns_id_and_writes_monthly_file(tmp_path):
    did = append_decision(tmp_path, _decision())
    assert isinstance(did, str) and did
    f = journal_file(tmp_path, datetime(2026, 5, 29, tzinfo=UTC))
    assert f.exists() and f.name == "journal-2026-05.jsonl"
    recs = read_all_decisions(tmp_path)
    assert len(recs) == 1 and recs[0]["id"] == did and recs[0]["symbol"] == "BTCUSDT"


def test_open_decisions_excludes_closed(tmp_path):
    d1 = append_decision(tmp_path, _decision(symbol="BTCUSDT"))
    append_decision(tmp_path, _decision(symbol="ETHUSDT"))
    assert len(read_open_decisions(tmp_path)) == 2
    ok = patch_outcome(tmp_path, d1, {
        "exit_ts": datetime(2026, 5, 30, tzinfo=UTC), "realized_pnl": 42.0,
        "fees": 1.0, "prediction_correct": True, "low_level_lesson": "read was right",
    })
    assert ok is True
    opens = read_open_decisions(tmp_path)
    assert len(opens) == 1 and opens[0]["symbol"] == "ETHUSDT"


def test_patch_merges_outcome_fields(tmp_path):
    did = append_decision(tmp_path, _decision())
    patch_outcome(tmp_path, did, {"realized_pnl": -10.0, "prediction_correct": False})
    rec = next(r for r in read_all_decisions(tmp_path) if r["id"] == did)
    assert rec["realized_pnl"] == -10.0
    assert rec["prediction_correct"] is False
    assert rec["rationale"] == "momentum breakout"  # Phase-1 field preserved


def test_patch_unknown_id_returns_false(tmp_path):
    append_decision(tmp_path, _decision())
    assert patch_outcome(tmp_path, "nonexistent", {"realized_pnl": 1.0}) is False


def test_decision_model_allows_extra_agent_fields():
    # Phase B agents attach extra fields; the model must tolerate them
    d = Decision(id="x", ts=datetime(2026, 5, 29, tzinfo=UTC), cycle=1,
                 symbol="BTCUSDT", direction="long", entry=100.0, stop=95.0,
                 bull_thesis="...", some_future_field=123)
    dumped = d.model_dump()
    assert dumped["bull_thesis"] == "..." and dumped["some_future_field"] == 123


def test_patch_cross_month_boundary(tmp_path):
    # opened in April...
    did = append_decision(tmp_path, _decision(ts=datetime(2026, 4, 30, 23, 0, tzinfo=UTC)))
    assert (tmp_path / "episodic" / "journal-2026-04.jsonl").exists()
    # ...closed in May (different month): patch must rewrite the APRIL file, not create May
    ok = patch_outcome(tmp_path, did, {
        "exit_ts": datetime(2026, 5, 1, 2, 0, tzinfo=UTC),
        "realized_pnl": 55.0, "prediction_correct": True,
    })
    assert ok is True
    assert not (tmp_path / "episodic" / "journal-2026-05.jsonl").exists()
    rec = next(r for r in read_all_decisions(tmp_path) if r["id"] == did)
    assert rec["realized_pnl"] == 55.0
    assert rec["rationale"] == "momentum breakout"  # Phase-1 preserved across the patch

from datetime import UTC, datetime

from futures_fund.journal import append_decision, patch_outcome
from futures_fund.lessons import read_lessons
from futures_fund.memory_layout import ensure_memory_layout
from futures_fund.reflect import record_lesson, reflection_payload


def _closed(memory_dir, symbol, pnl):
    did = append_decision(memory_dir, {
        "ts": datetime(2026, 5, 1, tzinfo=UTC), "cycle": 1, "symbol": symbol,
        "direction": "long", "entry": 100.0, "stop": 95.0, "regime": "high_vol_trend",
    })
    patch_outcome(memory_dir, did, {"realized_pnl": pnl, "prediction_correct": pnl > 0})


def test_reflection_payload_splits_winners_and_losers(tmp_path):
    ensure_memory_layout(tmp_path)
    _closed(tmp_path, "BTCUSDT", 50.0)
    _closed(tmp_path, "ETHUSDT", -30.0)
    _closed(tmp_path, "SOLUSDT", 20.0)
    payload = reflection_payload(tmp_path)
    assert len(payload["winners"]) == 2
    assert len(payload["losers"]) == 1
    assert payload["losers"][0]["symbol"] == "ETHUSDT"


def test_record_lesson_appends_candidate_with_provenance(tmp_path):
    ensure_memory_layout(tmp_path)
    lid = record_lesson(tmp_path, text="cut leverage in high-vol chop",
                        regime="high_vol_range", tags=["leverage", "vol"],
                        importance=7, provenance=["dec1", "dec2"],
                        ts=datetime(2026, 5, 2, tzinfo=UTC))
    lessons = read_lessons(tmp_path)
    assert len(lessons) == 1
    lz = lessons[0]
    assert lz.id == lid and lz.state == "candidate"
    assert lz.regime == "high_vol_range" and lz.provenance == ["dec1", "dec2"]
    # also mirrored to the human-readable lessons.md
    md = (tmp_path / "lessons" / "lessons.md").read_text()
    assert "cut leverage in high-vol chop" in md

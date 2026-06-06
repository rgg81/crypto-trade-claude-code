"""Improvement metrics (Pillar 3 IMPROVE): deployment rate, corpus two-sidedness, return trend."""
import json
from datetime import UTC, datetime


def _report(state_dir, cycle, opened=0, triggers_armed=0):
    d = state_dir / "cycle" / str(cycle)
    d.mkdir(parents=True, exist_ok=True)
    (d / "report.json").write_text(json.dumps({"opened": opened, "triggers_armed": triggers_armed}))


def test_deployment_rate_counts_active_cycles(tmp_path):
    from futures_fund.improvement import deployment_rate
    s = tmp_path / "s"
    _report(s, 1, opened=0)                 # idle
    _report(s, 2, opened=1)                 # opened -> active
    _report(s, 3, triggers_armed=2)         # armed -> active
    _report(s, 4, opened=0, triggers_armed=0)  # idle
    r = deployment_rate(s, last_n=10)
    assert r["cycles"] == 4 and r["active"] == 2
    assert r["deployment_rate"] == 0.5 and r["opens"] == 1


def test_deployment_rate_empty_is_zero(tmp_path):
    from futures_fund.improvement import deployment_rate
    r = deployment_rate(tmp_path / "s")
    assert r["deployment_rate"] == 0.0 and r["cycles"] == 0


def test_corpus_health_two_sided(tmp_path):
    from futures_fund.improvement import corpus_health
    from futures_fund.lessons import append_lesson
    mem = tmp_path / "m"
    now = datetime(2026, 6, 1, tzinfo=UTC)
    append_lesson(mem, {"text": "DO take the flush short", "polarity": "enabling"}, now)
    append_lesson(mem, {"text": "do NOT chase climax", "polarity": "restrictive"}, now)
    h = corpus_health(mem)
    assert h["total"] == 2 and h["enabling"] == 1 and h["restrictive"] == 1
    assert h["two_sided"] is True


def test_corpus_health_one_sided_flagged(tmp_path):
    from futures_fund.improvement import corpus_health
    from futures_fund.lessons import append_lesson
    mem = tmp_path / "m"
    now = datetime(2026, 6, 1, tzinfo=UTC)
    append_lesson(mem, {"text": "do NOT a", "polarity": "restrictive"}, now)
    append_lesson(mem, {"text": "do NOT b", "polarity": "restrictive"}, now)
    h = corpus_health(mem)
    assert h["two_sided"] is False and h["restrictive"] == 2 and h["enabling"] == 0


def test_return_trend_improving(tmp_path):
    from futures_fund.equity_log import record_equity
    from futures_fund.improvement import return_trend
    s = tmp_path / "s"
    eq = 10000.0
    # prior window flat, recent window rising -> improving
    for i in range(16):
        eq *= 1.0 if i < 8 else 1.01
        record_equity(s, datetime(2026, 6, 1, tzinfo=UTC), eq, cycle=i)
    r = return_trend(s, window=6)
    assert r["trend"] == "improving"


def test_improvement_panel_bundles(tmp_path):
    from futures_fund.improvement import improvement_panel
    _report(tmp_path / "s", 1, opened=1)
    panel = improvement_panel(tmp_path / "s", tmp_path / "m")
    assert "deployment" in panel and "corpus" in panel and "returns" in panel

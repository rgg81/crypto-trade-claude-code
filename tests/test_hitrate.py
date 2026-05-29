from futures_fund.hitrate import hit_rate, record_outcome


def test_unknown_agent_defaults_to_half(tmp_path):
    assert hit_rate(tmp_path, "watcher") == 0.5


def test_record_and_compute_hit_rate(tmp_path):
    for correct in [True, True, False, True]:   # 3/4
        record_outcome(tmp_path, "trend_analyst", correct)
    assert hit_rate(tmp_path, "trend_analyst") == 0.75


def test_rolling_window_keeps_only_recent(tmp_path):
    # 20 wins then 5 losses; window of 5 -> 0.0
    for _ in range(20):
        record_outcome(tmp_path, "a", True)
    for _ in range(5):
        record_outcome(tmp_path, "a", False)
    assert hit_rate(tmp_path, "a", window=5) == 0.0
    assert hit_rate(tmp_path, "a", window=25) == 20 / 25


def test_separate_agents_tracked_independently(tmp_path):
    record_outcome(tmp_path, "bull", True)
    record_outcome(tmp_path, "bear", False)
    assert hit_rate(tmp_path, "bull") == 1.0
    assert hit_rate(tmp_path, "bear") == 0.0

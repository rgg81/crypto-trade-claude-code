from futures_fund.graduation import deflated_sharpe_pvalue, graduation_verdict


def test_deflated_sharpe_pvalue_in_unit_interval():
    rets = [0.01, -0.005, 0.02, 0.0, 0.015, -0.01, 0.012] * 5
    p = deflated_sharpe_pvalue(rets, num_trials=10)
    assert 0.0 <= p <= 1.0


def test_deflated_sharpe_pvalue_empty_is_zero():
    assert deflated_sharpe_pvalue([], num_trials=5) == 0.0


def test_verdict_graduated_when_all_criteria_met():
    v = graduation_verdict(n_cycles=30, sharpe=2.0, dsr_pvalue=0.97, beats_baseline=True,
                           max_dd=0.08, min_cycles=20, horizon_cycles=120)
    assert v["status"] == "graduated"
    assert v["reasons"] == []


def test_verdict_not_yet_lists_failing_criteria():
    v = graduation_verdict(n_cycles=10, sharpe=-0.5, dsr_pvalue=0.5, beats_baseline=False,
                           max_dd=0.2, min_cycles=20, horizon_cycles=120)
    assert v["status"] == "not_yet"
    assert any("cycles" in r for r in v["reasons"])
    assert any("DSR" in r for r in v["reasons"])
    assert any("baseline" in r for r in v["reasons"])


def test_verdict_failed_past_horizon_without_edge():
    v = graduation_verdict(n_cycles=130, sharpe=0.1, dsr_pvalue=0.4, beats_baseline=False,
                           max_dd=0.2, min_cycles=20, horizon_cycles=120)
    assert v["status"] == "failed"

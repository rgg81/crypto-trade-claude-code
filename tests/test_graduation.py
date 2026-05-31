from futures_fund.graduation import deflated_sharpe_pvalue, graduation_verdict


def test_deflated_sharpe_pvalue_in_unit_interval():
    rets = [0.01, -0.005, 0.02, 0.0, 0.015, -0.01, 0.012] * 5
    p = deflated_sharpe_pvalue(rets, num_trials=10)
    assert 0.0 <= p <= 1.0


def test_deflated_sharpe_pvalue_empty_is_zero():
    assert deflated_sharpe_pvalue([], num_trials=5) == 0.0


# --- sigma_SR fix: the gate must be REACHABLE for a strong desk yet still BLOCK weak ones ----

_STRONG = [0.015, -0.005] * 60   # 120 cycles, realistic per-period Sharpe ~0.5


def test_dsr_reachable_for_a_strong_desk():
    # A genuine, realistic edge over enough cycles must be able to clear 0.95.
    # (The old vendor omitted sigma_SR and returned ~0 here — the gate was unreachable.)
    assert deflated_sharpe_pvalue(_STRONG, num_trials=10) >= 0.95


def test_dsr_blocks_zero_and_negative_edge():
    assert deflated_sharpe_pvalue([0.01, -0.01] * 30, num_trials=10) < 0.95          # mean ~0
    assert deflated_sharpe_pvalue([-0.005, 0.001, -0.003, 0.002] * 15, num_trials=10) < 0.95


def test_dsr_blocks_strong_edge_on_a_thin_sample():
    # Even a strong mean is not trusted on too few observations.
    assert deflated_sharpe_pvalue([0.015, -0.005] * 6, num_trials=10) < 0.95  # 12 cycles


def test_dsr_explicit_sigma_sr_overrides_reduction():
    # the old broken behaviour (sigma_SR=1.0 per-period) makes a great desk unreachable
    assert deflated_sharpe_pvalue(_STRONG, num_trials=10, sigma_sr=1.0) < 0.05
    # a tracked, realistic per-period sigma_SR keeps it reachable
    assert deflated_sharpe_pvalue(_STRONG, num_trials=10, sigma_sr=0.05) > 0.9


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

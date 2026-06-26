"""Monthly risk-pacing engine (Pillar 1 DEPLOY): start soft -> press when behind pace (and NOT in
drawdown) -> throttle once the 5%/month target is hit. Anti-martingale: drawdown ALWAYS suppresses
press (the desk presses with UNUSED budget, never into losses)."""
from datetime import UTC, datetime

from futures_fund.pacing import (
    CAUTION_DD,
    PRESS_GAP,
    compute_pacing,
    pacing_state,
)


def _c(mtd, day, dd=0.0, heat=0.0, dim=30, target=0.05):
    # day = days elapsed in the month (1.0 = end of the 1st)
    return compute_pacing(mtd_return=mtd, days_elapsed=day, days_in_month=dim,
                          drawdown=dd, open_heat=heat, monthly_target=target)


def test_throttle_when_target_hit():
    s = _c(mtd=0.05, day=10)
    assert s.mode == "throttle"
    assert s.suggested_risk_mult <= 0.6
    s2 = _c(mtd=0.061, day=2)  # hit early -> still throttle
    assert s2.mode == "throttle"


def test_soft_early_month():
    s = _c(mtd=0.0, day=2)  # day 2 < SOFT_DAYS, behind but early -> soft, NOT press
    assert s.mode == "soft"


def test_press_when_behind_and_underdeployed_and_no_drawdown():
    # day 15 of 30, pace = 2.5%, mtd 0% -> gap -2.5% (> PRESS_GAP behind), no dd, low heat -> press
    s = _c(mtd=0.0, day=15, dd=0.0, heat=0.0)
    assert s.mode == "press"
    assert s.suggested_risk_mult >= 0.95
    assert s.appetite >= 0.8


def test_anti_martingale_drawdown_never_presses():
    # same behind-pace setup BUT in drawdown -> must NOT press (breakers own the loss path)
    s = _c(mtd=-0.04, day=15, dd=CAUTION_DD + 0.001, heat=0.0)
    assert s.mode != "press"
    assert s.mode == "soft"
    assert s.in_drawdown is True
    assert s.suggested_risk_mult <= 0.6


def test_no_press_when_already_deployed():
    # behind pace but heat already high (deployed) -> not under-deployed -> normal, not press
    s = _c(mtd=0.0, day=15, dd=0.0, heat=0.06)
    assert s.mode == "normal"


def test_normal_when_on_pace():
    # day 15, pace 2.5%, mtd 2.5% -> on pace -> normal
    s = _c(mtd=0.025, day=15, dd=0.0, heat=0.0)
    assert s.mode == "normal"


def test_pace_gap_sign_and_fields():
    s = _c(mtd=0.0, day=15, dim=30, target=0.05)
    assert abs(s.pace - 0.025) < 1e-9      # 0.05 * 15/30
    assert abs(s.pace_gap - (-0.025)) < 1e-9
    assert s.mtd_return == 0.0
    assert isinstance(s.directive, str) and len(s.directive) > 0


def test_press_requires_gap_beyond_threshold():
    # only slightly behind (< PRESS_GAP) -> normal, not press
    s = _c(mtd=0.025 - (PRESS_GAP * 0.5), day=15)
    assert s.mode == "normal"


def test_pacing_state_reads_equity_log(tmp_path):
    from futures_fund.equity_log import record_equity
    state = tmp_path / "s"
    # month-start anchor 10000 on Jun 1, latest 10000 on Jun 16 -> mtd 0%, day 15, behind -> press
    record_equity(state, datetime(2026, 6, 1, tzinfo=UTC), 10000.0, cycle=1)
    record_equity(state, datetime(2026, 6, 16, tzinfo=UTC), 10000.0, cycle=2)

    class _H:
        drawdown_from_peak = 0.0
        open_heat = 0.0
    s = pacing_state(state, datetime(2026, 6, 16, tzinfo=UTC), _H(), monthly_target=0.05)
    assert s.mode == "press"
    assert abs(s.mtd_return - 0.0) < 1e-9


def test_pacing_state_empty_log_is_soft(tmp_path):
    class _H:
        drawdown_from_peak = 0.0
        open_heat = 0.0
    s = pacing_state(tmp_path / "s", datetime(2026, 6, 16, tzinfo=UTC), _H())
    assert s.mode == "soft"  # no data -> conservative default


def test_pacing_state_mtd_from_month_start_anchor(tmp_path):
    from futures_fund.equity_log import record_equity
    state = tmp_path / "s"
    record_equity(state, datetime(2026, 5, 20, tzinfo=UTC), 9000.0, cycle=1)   # prior month
    record_equity(state, datetime(2026, 6, 1, tzinfo=UTC), 10000.0, cycle=2)   # month-start anchor
    record_equity(state, datetime(2026, 6, 10, tzinfo=UTC), 10300.0, cycle=3)  # +3% MTD

    class _H:
        drawdown_from_peak = 0.0
        open_heat = 0.0
    s = pacing_state(state, datetime(2026, 6, 10, tzinfo=UTC), _H(), monthly_target=0.05)
    assert abs(s.mtd_return - 0.03) < 1e-6   # vs the Jun-1 anchor, not the May point


# --- dual-anchor pace_gap: calendar MTD cannot mask multi-month (since-seed) lag ---

def test_dual_anchor_seed_gap_drives_press_when_calendar_flatters():
    # The keystone bug: calendar reads ON-PACE (mtd +4.78%, day 26 -> pace +4.33%, gap +0.45%) off a
    # prior-month LOW, but SINCE-SEED is behind (+3.21% over 56d vs the +9.33% sustained pace ->
    # seed_gap -6.1%). The operative gap must be the MORE-BEHIND seed gap, so a healthy under-
    # deployed desk PRESSES instead of coasting "on pace".
    s = compute_pacing(mtd_return=0.0478, days_elapsed=26, days_in_month=30, drawdown=0.0,
                       open_heat=0.0, monthly_target=0.05,
                       since_seed_return=0.0321, days_since_seed=56)
    calendar_gap = 0.0478 - 0.05 * 26 / 30
    assert s.seed_pace_gap is not None and s.seed_pace_gap < -PRESS_GAP
    assert abs(s.pace_gap - min(calendar_gap, s.seed_pace_gap)) < 1e-12
    assert s.mode == "press"


def test_dual_anchor_calendar_gap_used_when_more_behind():
    # Symmetry: when the CALENDAR gap is the more-behind one, it wins the min().
    s = compute_pacing(mtd_return=-0.03, days_elapsed=15, days_in_month=30, drawdown=0.0,
                       open_heat=0.0, monthly_target=0.05,
                       since_seed_return=0.02, days_since_seed=20)
    cal = -0.03 - 0.05 * 15 / 30            # -0.055
    seed = 0.02 - 0.05 * 20 / 30            # -0.0133
    assert abs(s.pace_gap - min(cal, seed)) < 1e-12
    assert abs(s.pace_gap - cal) < 1e-12


def test_dual_anchor_absent_is_calendar_only_backcompat():
    # No since-seed inputs -> behaviour is exactly the legacy calendar-only gap.
    s = compute_pacing(mtd_return=0.01, days_elapsed=15, days_in_month=30, drawdown=0.0,
                       open_heat=0.0, monthly_target=0.05)
    assert s.seed_pace_gap is None
    assert abs(s.pace_gap - (0.01 - 0.025)) < 1e-12


def test_dual_anchor_never_overrides_anti_martingale():
    # Deeply behind on BOTH anchors BUT in drawdown -> still soft, never press (invariant kept).
    s = compute_pacing(mtd_return=-0.04, days_elapsed=15, days_in_month=30,
                       drawdown=CAUTION_DD + 0.001, open_heat=0.0, monthly_target=0.05,
                       since_seed_return=-0.05, days_since_seed=40)
    assert s.mode == "soft"
    assert s.in_drawdown is True


def test_dual_anchor_throttle_still_protects_a_strong_month():
    # Calendar month ALREADY hit target -> throttle (bank/protect), even if since-seed lags.
    s = compute_pacing(mtd_return=0.052, days_elapsed=20, days_in_month=30, drawdown=0.0,
                       open_heat=0.0, monthly_target=0.05,
                       since_seed_return=0.03, days_since_seed=50)
    assert s.mode == "throttle"


def test_dual_anchor_directive_surfaces_sustained_line():
    s = compute_pacing(mtd_return=0.0478, days_elapsed=26, days_in_month=30, drawdown=0.0,
                       open_heat=0.0, monthly_target=0.05,
                       since_seed_return=0.0321, days_since_seed=56)
    low = s.directive.lower()
    assert "sustained" in low or "since-seed" in low


def test_pacing_state_dual_anchor_presses_on_sustained_lag(tmp_path):
    from futures_fund.equity_log import record_equity
    state = tmp_path / "s"
    # seed 10000 (May 1), dipped to 9850 end-May (-1.5%), June climbs to 10320 (+4.78% MTD off the
    # May low). Calendar alone reads on-pace; since-seed is only +3.2% over ~55d vs the +9.2%
    # sustained pace -> behind -> dual-anchor presses.
    record_equity(state, datetime(2026, 5, 1, tzinfo=UTC), 10000.0, cycle=1)
    record_equity(state, datetime(2026, 5, 31, tzinfo=UTC), 9850.0, cycle=2)
    record_equity(state, datetime(2026, 6, 25, tzinfo=UTC), 10320.0, cycle=3)

    class _H:
        drawdown_from_peak = 0.0
        open_heat = 0.0
    s = pacing_state(state, datetime(2026, 6, 25, tzinfo=UTC), _H(), monthly_target=0.05)
    assert s.seed_pace_gap is not None and s.seed_pace_gap < 0
    assert s.mode == "press"

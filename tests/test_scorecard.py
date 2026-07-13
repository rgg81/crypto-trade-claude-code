import json
from datetime import UTC, datetime, timedelta

from futures_fund.equity_log import record_equity
from futures_fund.journal import append_decision, patch_outcome
from futures_fund.memory_layout import ensure_memory_layout
from futures_fund.scorecard import build_scorecard


def _seed(state_dir, memory_dir):
    ensure_memory_layout(memory_dir)
    for i, eq in enumerate([10_000, 10_200, 10_100, 10_500], start=1):
        record_equity(state_dir, datetime(2026, 5, 1, 4 * i, tzinfo=UTC), float(eq), cycle=i)
    # 3 distinct closed trades on DISTINCT cycles — the desk opens one BTCUSDT-long per cycle, never
    # three in one cycle; identical (cycle, symbol, direction) is a RETRY duplicate and is deduped.
    for c, (pnl, agents) in enumerate([(200.0, ["team"]), (-100.0, ["team"]), (400.0, ["team"])], start=1):
        did = append_decision(memory_dir, {"ts": datetime(2026, 5, 1, tzinfo=UTC), "cycle": c,
                                           "symbol": "BTCUSDT", "direction": "long",
                                           "entry": 100.0, "stop": 95.0,
                                           "contributing_agents": agents})
        patch_outcome(memory_dir, did, {"realized_pnl": pnl, "prediction_correct": pnl > 0})


def test_scorecard_has_headline_stats_and_target(tmp_path):
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    _seed(state_dir, memory_dir)
    sc = build_scorecard(state_dir, memory_dir, monthly_target=0.05)
    assert sc["equity"] == 10_500.0
    assert sc["monthly_target"] == 0.05
    assert "sharpe" in sc and "max_drawdown" in sc and "hit_rate" in sc
    assert sc["n_closed"] == 3
    assert sc["hit_rate"] > 0.5  # 2 of 3 wins
    assert "team" in sc["agent_hit_rates"]
    assert sc["graduation"]["status"] in {"graduated", "not_yet", "failed"}


def test_scorecard_warns_in_drawdown(tmp_path):
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    ensure_memory_layout(memory_dir)
    for i, eq in enumerate([10_000, 9_000], start=1):  # -10% drawdown
        record_equity(state_dir, datetime(2026, 5, 1, 4 * i, tzinfo=UTC), float(eq), cycle=i)
    sc = build_scorecard(state_dir, memory_dir, monthly_target=0.05)
    assert any("drawdown" in w.lower() for w in sc["warnings"])


def test_scorecard_empty_history_is_safe(tmp_path):
    sc = build_scorecard(tmp_path / "s", tmp_path / "m", monthly_target=0.05)
    assert sc["equity"] is None and sc["n_closed"] == 0


def test_dsr_not_collapsed_by_per_symbol_trade_dispersion(tmp_path):
    """REGRESSION (cy223 bug): the scorecard fed the DSR a sigma_SR computed from per-SYMBOL
    per-TRADE Sharpes — a scale incommensurable with the per-CYCLE portfolio Sharpe (a ~22x unit
    mismatch on live data) — which inflated expected_max_SR and collapsed the DSR p-value to ~0
    (4e-257), FALSELY reporting 'no edge' on a genuinely positive track record. The DSR must use
    the canonical single-strategy reduction (sigma_SR = the Sharpe's own standard error) with the
    fixed num_trials deflation, so a positive-return desk gets a sane p-value, not a degenerate ~0.
    """
    from futures_fund.graduation import deflated_sharpe_pvalue
    from futures_fund.scorecard import returns_series

    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    ensure_memory_layout(memory_dir)
    # >=11 returns of a gently-rising-but-noisy equity -> a modest positive per-cycle Sharpe.
    eqs = [10_000, 10_120, 10_060, 10_180, 10_110, 10_250, 10_190, 10_320,
           10_260, 10_400, 10_330, 10_470, 10_410, 10_550, 10_500, 10_620]
    for i, eq in enumerate(eqs, start=1):
        record_equity(state_dir, datetime(2026, 5, 1, tzinfo=UTC) + timedelta(hours=4 * i),
                      float(eq), cycle=i)
    # Two symbols whose per-TRADE Sharpes are wildly different: a very-consistent small-winner
    # (tiny within-symbol std -> huge per-trade Sharpe) and a dispersed mixed stream. Their
    # cross-symbol sigma_SR is >> the portfolio Sharpe's standard error; fed to the DSR (the bug)
    # it collapses the p-value. Distinct cycles per trade (identical cycle+symbol+dir is deduped).
    streams = {"AAAUSDT": [0.020, 0.021, 0.019, 0.020, 0.0205],
               "BBBUSDT": [0.05, -0.04, 0.03, -0.02, 0.01]}
    c = 1
    for sym, stream in streams.items():
        for r in stream:
            did = append_decision(memory_dir, {"ts": datetime(2026, 5, 1, tzinfo=UTC), "cycle": c,
                                               "symbol": sym, "direction": "long",
                                               "entry": 100.0, "stop": 95.0, "size": 1.0,
                                               "contributing_agents": ["team"]})
            patch_outcome(memory_dir, did, {"realized_pnl": r * 100.0, "prediction_correct": r > 0})
            c += 1
    sc = build_scorecard(state_dir, memory_dir, monthly_target=0.05)
    # A clean positive track record must yield a SANE DSR p-value, never a collapsed ~0.
    assert sc["dsr_pvalue"] > 0.05, (
        f"DSR collapsed to {sc['dsr_pvalue']:.2e} (per-trade sigma_SR unit mismatch)")
    # The graduation reason must not read the degenerate 'DSR 0.00'.
    assert not any("DSR 0.00" in r for r in sc["graduation"]["reasons"])
    # It must equal the single-strategy (sigma_sr=None) reduction on the same per-cycle returns.
    rets = returns_series(state_dir)
    assert sc["dsr_pvalue"] == deflated_sharpe_pvalue(rets, num_trials=10)


# ───────────────────── A+B: rebalanced (two-sided) scorecard signals ─────────────────────

def _seed_idle_tradeable(state_dir, memory_dir, *, opened_recent=0, screened=("XLMUSDT",), n=7,
                         equities=None, halted=False, positions=None):
    """A healthy, FLAT desk that keeps screening candidates but isn't trading — the exact state
    that should trigger the under-deployment counter-signal."""
    ensure_memory_layout(memory_dir)
    eqs = equities or [9990 + (i % 3) for i in range(n)]  # flat, ~0 drawdown, healthy tier
    base = datetime(2026, 5, 1, tzinfo=UTC)
    for i, eq in enumerate(eqs, start=1):
        record_equity(state_dir, base + timedelta(hours=4 * i), float(eq), cycle=i)
    for i in range(1, n + 1):
        d = state_dir / "cycle" / str(i)
        d.mkdir(parents=True, exist_ok=True)
        op = opened_recent if i >= n - 1 else 0  # opens only in the last 2 cycles
        (d / "report.json").write_text(json.dumps({"cycle": i, "opened": op}))
    if screened:
        (state_dir / "cycle" / str(n) / "screened.json").write_text(
            json.dumps({"symbols": list(screened)}))
    if positions:
        (state_dir / "positions.json").write_text(json.dumps(positions))
    if halted:
        (state_dir / "account.json").write_text(json.dumps({"balance": 9990.0, "halt": True}))


def test_under_deployment_signal_fires_when_idle_with_candidates(tmp_path):
    s, m = tmp_path / "s", tmp_path / "m"
    _seed_idle_tradeable(s, m)
    sc = build_scorecard(s, m, monthly_target=0.05)
    assert any("under-deployed" in w for w in sc["warnings"]), sc["warnings"]


def test_under_deployment_silent_when_holding_positions(tmp_path):
    s, m = tmp_path / "s", tmp_path / "m"
    _seed_idle_tradeable(s, m, positions=[{"symbol": "BTCUSDT"}])
    sc = build_scorecard(s, m, monthly_target=0.05)
    assert not any("under-deployed" in w for w in sc["warnings"])


def test_under_deployment_silent_when_recent_opens(tmp_path):
    s, m = tmp_path / "s", tmp_path / "m"
    _seed_idle_tradeable(s, m, opened_recent=1)  # traded in the last 2 cycles
    sc = build_scorecard(s, m, monthly_target=0.05)
    assert not any("under-deployed" in w for w in sc["warnings"])


def test_under_deployment_silent_when_no_candidates(tmp_path):
    s, m = tmp_path / "s", tmp_path / "m"
    _seed_idle_tradeable(s, m, screened=())  # thin tape — nothing to deploy into
    sc = build_scorecard(s, m, monthly_target=0.05)
    assert not any("under-deployed" in w for w in sc["warnings"])


def test_under_deployment_silent_in_drawdown(tmp_path):
    s, m = tmp_path / "s", tmp_path / "m"
    # peak 10000 -> current 9000 = -10% current drawdown (not healthy): accelerator must stay off
    _seed_idle_tradeable(s, m, equities=[10000, 9800, 9600, 9400, 9200, 9100, 9000])
    sc = build_scorecard(s, m, monthly_target=0.05)
    assert not any("under-deployed" in w for w in sc["warnings"])
    assert any("drawdown" in w.lower() for w in sc["warnings"])  # the brake still fires


def test_under_deployment_silent_when_halted(tmp_path):
    s, m = tmp_path / "s", tmp_path / "m"
    _seed_idle_tradeable(s, m, halted=True)
    sc = build_scorecard(s, m, monthly_target=0.05)
    assert not any("under-deployed" in w for w in sc["warnings"])


def test_below_target_line_is_two_sided_not_pure_brake(tmp_path):
    s, m = tmp_path / "s", tmp_path / "m"
    # mildly underwater recent pace, healthy tier
    _seed_idle_tradeable(s, m, equities=[10000, 9970, 9950, 9960, 9955, 9950, 9948])
    sc = build_scorecard(s, m, monthly_target=0.05)
    pace = [w for w in sc["warnings"] if "pace" in w or "proven-edge" in w]
    assert pace, sc["warnings"]
    # the reworded line must NOT be the old unconditional 'do not force trades' brake
    assert not any(w == "running below the 5%/mo target — do not force trades" for w in sc["warnings"])
    assert any("do not stand flat" in w or "not forcing" in w.lower() for w in pace)

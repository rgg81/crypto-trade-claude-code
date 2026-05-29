from datetime import UTC, datetime

from futures_fund.equity_log import record_equity
from futures_fund.journal import append_decision, patch_outcome
from futures_fund.memory_layout import ensure_memory_layout
from futures_fund.scorecard import build_scorecard


def _seed(state_dir, memory_dir):
    ensure_memory_layout(memory_dir)
    for i, eq in enumerate([10_000, 10_200, 10_100, 10_500], start=1):
        record_equity(state_dir, datetime(2026, 5, 1, 4 * i, tzinfo=UTC), float(eq), cycle=i)
    for pnl, agents in [(200.0, ["team"]), (-100.0, ["team"]), (400.0, ["team"])]:
        did = append_decision(memory_dir, {"ts": datetime(2026, 5, 1, tzinfo=UTC), "cycle": 1,
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

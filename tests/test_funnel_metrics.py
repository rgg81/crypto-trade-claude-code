"""Funnel conversion: measure the EDGE -> EXECUTION pipeline, not the cash balance.

cy313 diagnosed the desk's under-deployment by counting, not by intuition: across cy295-313 it
proposed ZERO market entries in 19 cycles and arm-attempted 8 setups against 53 edge-aligned
declines — a ~6% conversion. A cash-deployment quota would have "fixed" that by forcing negative-
expectancy trades; measuring the funnel finds the BLOCKAGE instead (there, a market-entry path
gated on an ADX bar nothing ever printed).
"""
import json

import pytest

from futures_fund.funnel_metrics import funnel_block, read_funnel_stats


def _report(state_dir, n, *, armed=0, market=0, opened=0):
    d = state_dir / "cycle" / str(n)
    d.mkdir(parents=True, exist_ok=True)
    (d / "report.json").write_text(json.dumps(
        {"cycle": n, "triggers_armed": armed, "market_entries": market, "opened": opened}))


def _declines(memory_dir, rows):
    memory_dir.mkdir(parents=True, exist_ok=True)
    (memory_dir / "flat-decisions.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows))


def test_healthy_funnel_reports_high_conversion_and_no_alert(tmp_path):
    state, mem = tmp_path / "state", tmp_path / "memory"
    for n in (10, 11, 12):
        _report(state, n, armed=2)
    _declines(mem, [{"cycle": n, "edge_aligned": True} for n in (10, 11, 12)])
    s = read_funnel_stats(state, mem, window=12)
    assert s.armed == 6 and s.edge_declined == 3
    assert s.acted == 6 and s.identified == 9
    assert s.conversion == pytest.approx(6 / 9)
    assert s.stalled_streak == 0
    assert "ALERT" not in funnel_block(s)


def test_blocked_funnel_is_detected_and_alerts(tmp_path):
    """The cy295-313 signature: edge is found every cycle and NOTHING is acted on."""
    state, mem = tmp_path / "state", tmp_path / "memory"
    rows = []
    for n in range(10, 15):
        _report(state, n, armed=0, market=0)
        rows += [{"cycle": n, "edge_aligned": True} for _ in range(3)]
    _declines(mem, rows)
    s = read_funnel_stats(state, mem, window=12)
    assert s.acted == 0 and s.edge_declined == 15
    assert s.conversion == pytest.approx(0.0)
    assert s.stalled_streak == 5              # every recent cycle found edge and armed nothing
    blk = funnel_block(s)
    assert "ALERT" in blk and "0%" in blk


def test_market_entries_and_opens_count_as_acted(tmp_path):
    state, mem = tmp_path / "state", tmp_path / "memory"
    _report(state, 20, armed=1, market=2)
    _declines(mem, [{"cycle": 20, "edge_aligned": True}])
    s = read_funnel_stats(state, mem, window=12)
    assert s.market_entries == 2 and s.acted == 3
    assert s.conversion == pytest.approx(3 / 4)


def test_non_edge_aligned_declines_do_not_count_against_the_desk(tmp_path):
    """Declining a setup that never had the desk's edge is correct, not a missed conversion."""
    state, mem = tmp_path / "state", tmp_path / "memory"
    _report(state, 30, armed=1)
    _declines(mem, [{"cycle": 30, "edge_aligned": False} for _ in range(9)]
                   + [{"cycle": 30, "edge_aligned": True}])
    s = read_funnel_stats(state, mem, window=12)
    assert s.edge_declined == 1 and s.conversion == pytest.approx(0.5)


def test_window_limits_the_lookback(tmp_path):
    state, mem = tmp_path / "state", tmp_path / "memory"
    for n in range(1, 21):
        _report(state, n, armed=1)
    _declines(mem, [{"cycle": n, "edge_aligned": True} for n in range(1, 21)])
    s = read_funnel_stats(state, mem, window=5)
    assert s.cycles == 5 and s.armed == 5 and s.edge_declined == 5


def test_a_stall_streak_only_counts_cycles_that_actually_found_edge(tmp_path):
    """A genuinely dry board (no edge found) is NOT a blockage — it must not trip the alert."""
    state, mem = tmp_path / "state", tmp_path / "memory"
    for n in (40, 41, 42, 43):
        _report(state, n, armed=0)
    _declines(mem, [])                        # nothing edge-aligned was ever identified
    s = read_funnel_stats(state, mem, window=12)
    assert s.edge_declined == 0 and s.stalled_streak == 0
    assert s.conversion is None               # undefined, not zero
    assert "ALERT" not in funnel_block(s)


def test_degrades_safely_on_missing_or_corrupt_inputs(tmp_path):
    state, mem = tmp_path / "state", tmp_path / "memory"
    s = read_funnel_stats(state, mem, window=12)          # nothing exists at all
    assert s.cycles == 0 and s.acted == 0 and s.conversion is None
    assert isinstance(funnel_block(s), str)
    (state / "cycle" / "50").mkdir(parents=True)
    (state / "cycle" / "50" / "report.json").write_text("{not json")
    mem.mkdir(parents=True, exist_ok=True)
    (mem / "flat-decisions.jsonl").write_text("garbage\n{}\n")
    s2 = read_funnel_stats(state, mem, window=12)         # must not raise
    assert isinstance(funnel_block(s2), str)

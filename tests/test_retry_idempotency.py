"""P1 fix: a DUE RETRY re-running the same cycle must NOT duplicate equity points or double-journal
opens — those corrupt the return series (Sharpe/Sortino + circuit breakers) and the hit-rate stats."""
from datetime import UTC, datetime

from futures_fund.equity_log import equity_series, record_equity, returns_series
from futures_fund.journal import append_decision, read_all_decisions

_T0 = datetime(2026, 3, 1, tzinfo=UTC)
_T1 = datetime(2026, 3, 1, 4, tzinfo=UTC)


def test_record_equity_replaces_on_retry_not_duplicates(tmp_path):
    record_equity(tmp_path, _T0, 10_000.0, cycle=1)
    record_equity(tmp_path, _T1, 10_120.0, cycle=2)
    # cycle 2 crashed after recording; DUE RETRY re-runs cycle 2 with a (corrected) equity
    record_equity(tmp_path, _T1, 10_100.0, cycle=2)
    series = equity_series(tmp_path)
    assert len(series) == 2                       # NOT 3 — the retry replaced, didn't duplicate
    assert series[-1][1] == 10_100.0              # the retry's value won
    # the return series has ONE step (cyc1->cyc2), no spurious ~0% duplicate-cycle step
    assert len(returns_series(tmp_path)) == 1


def test_record_equity_distinct_cycles_accumulate(tmp_path):
    for c, eq in [(1, 10_000.0), (2, 10_100.0), (3, 10_050.0)]:
        record_equity(tmp_path, _T0, eq, cycle=c)
    assert len(equity_series(tmp_path)) == 3 and len(returns_series(tmp_path)) == 2


def _open(cycle, symbol="BTCUSDT", direction="long"):
    return {"ts": _T0, "cycle": cycle, "symbol": symbol, "direction": direction, "entry": 100.0,
            "stop": 95.0, "take_profit": [110.0], "size": 1.0, "leverage": 2.0}


def test_append_decision_idempotent_on_retry(tmp_path):
    id1 = append_decision(tmp_path, _open(cycle=5))
    id2 = append_decision(tmp_path, _open(cycle=5))  # DUE RETRY re-journals the same open
    assert id1 == id2                                 # same id returned, not a new one
    assert len(read_all_decisions(tmp_path)) == 1     # only ONE decision on disk


def test_append_decision_distinct_opens_kept(tmp_path):
    append_decision(tmp_path, _open(cycle=5, symbol="BTCUSDT", direction="long"))
    append_decision(tmp_path, _open(cycle=5, symbol="ETHUSDT", direction="short"))  # diff symbol/dir
    append_decision(tmp_path, _open(cycle=9, symbol="BTCUSDT", direction="long"))   # diff cycle
    assert len(read_all_decisions(tmp_path)) == 3     # all distinct -> all kept


def test_append_decision_same_symbol_both_directions_kept(tmp_path):
    # market-neutral: holding BTC long AND BTC short in the same cycle are DISTINCT decisions
    append_decision(tmp_path, _open(cycle=7, symbol="BTCUSDT", direction="long"))
    append_decision(tmp_path, _open(cycle=7, symbol="BTCUSDT", direction="short"))
    assert len(read_all_decisions(tmp_path)) == 2

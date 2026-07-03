"""Balance-vs-journal reconciliation (cy94 +$87 residual investigation).

Root cause found: balance = seed - Σentry_fee + Σrealized_final + Σscale_out_banks. The journal's
realized view omits (a) entry fees (booked to balance at open, never in realized_pnl) and (b) any
scale-out bank that predates the cy78 partial_banks fix (the cy22 SOL +$119.45 was credited to
balance but never journaled). reconcile_balance makes the full attribution and surfaces a residual;
unrecorded_banks detects report-confirmed banks missing from the journal so they can be backfilled.
"""
from futures_fund.costs import TAKER_RATE
from futures_fund.reconcile import (
    bank_total,
    entry_fee_for,
    match_open_decision,
    reconcile_balance,
    unrecorded_banks,
)


def test_entry_fee_for_uses_taker_on_qty_times_entry():
    d = {"size": 49.0, "entry": 80.0}
    assert entry_fee_for(d) == 49.0 * 80.0 * TAKER_RATE


def test_entry_fee_for_missing_fields_is_zero():
    assert entry_fee_for({"size": None, "entry": 80.0}) == 0.0
    assert entry_fee_for({"entry": 80.0}) == 0.0
    assert entry_fee_for({}) == 0.0


def test_bank_total_sums_partial_bank_pnl():
    d = {"partial_banks": [{"pnl": 119.45}, {"pnl": 10.0}]}
    assert bank_total(d) == 129.45


def test_bank_total_empty_or_none_is_zero():
    assert bank_total({"partial_banks": []}) == 0.0
    assert bank_total({"partial_banks": None}) == 0.0
    assert bank_total({}) == 0.0


def test_reconcile_balance_closes_with_banks_and_entry_fees():
    # one closed trade: realized_final 152.21, a 119.45 scale-out bank, entry fee on 49*80 notional.
    decisions = [{
        "realized_pnl": 152.21, "size": 49.0, "entry": 80.0,
        "partial_banks": [{"pnl": 119.45}],
    }]
    seed = 10000.0
    ef = 49.0 * 80.0 * TAKER_RATE  # 1.96
    expected = seed + 152.21 + 119.45 - ef
    r = reconcile_balance(decisions, balance=expected, seed=seed)
    assert abs(r["residual"]) < 1e-6
    assert r["realized_final"] == 152.21
    assert r["scale_out_banks"] == 119.45
    assert abs(r["entry_fees"] - ef) < 1e-9


def test_reconcile_balance_surfaces_unjournaled_bank_as_residual():
    # SAME trade but the bank is NOT in the journal (partial_banks empty) -> residual = the bank.
    decisions = [{
        "realized_pnl": 152.21, "size": 49.0, "entry": 80.0, "partial_banks": [],
    }]
    seed = 10000.0
    ef = 49.0 * 80.0 * TAKER_RATE
    actual = seed + 152.21 + 119.45 - ef  # balance really got the bank
    r = reconcile_balance(decisions, balance=actual, seed=seed)
    assert abs(r["residual"] - 119.45) < 1e-6  # the missing bank shows up as the residual


def test_reconcile_ignores_open_decisions():
    decisions = [
        {"realized_pnl": 100.0, "size": 10.0, "entry": 50.0},   # closed
        {"realized_pnl": None, "size": 10.0, "entry": 50.0},    # still open -> excluded
    ]
    r = reconcile_balance(decisions, balance=10000.0, seed=10000.0)
    assert r["realized_final"] == 100.0  # only the closed one


def test_reconcile_accounts_for_open_position_entry_fees():
    # balance was debited the entry fee when the open position was opened, but the reconcile's
    # closed-only sum excludes it -> without accounting the residual is a spurious -entry_fee.
    # A position uses `qty` (not `size`); entry_fee_for must handle both.
    open_positions = [{"qty": 10.0, "entry": 50.0}]  # notional 500
    ef_open = 10.0 * 50.0 * TAKER_RATE
    balance = 10000.0 - ef_open  # balance really got debited the open entry fee
    # WITHOUT open_positions: spurious residual of -ef_open (the false-positive we are killing)
    r_blind = reconcile_balance([], balance=balance, seed=10000.0)
    assert abs(r_blind["residual"] + ef_open) < 1e-9
    # WITH open_positions: expected includes the open entry fee -> residual ~0, reconciled clean
    r = reconcile_balance([], balance=balance, seed=10000.0, open_positions=open_positions)
    assert abs(r["open_entry_fees"] - ef_open) < 1e-9
    assert abs(r["residual"]) < 1e-9


def test_entry_fee_for_handles_position_qty_key():
    # a Position dict carries `qty`; a closed decision carries `size`. Both must price identically.
    from_qty = entry_fee_for({"qty": 10.0, "entry": 50.0})
    from_size = entry_fee_for({"size": 10.0, "entry": 50.0})
    assert from_qty == from_size == 10.0 * 50.0 * TAKER_RATE


def test_match_open_decision_interval_containment():
    decisions = [
        {"id": "a", "symbol": "SOLUSDT", "opened_ts": "2026-06-01T00:00:00Z",
         "exit_ts": "2026-06-03T04:00:00Z"},
        {"id": "b", "symbol": "SOLUSDT", "opened_ts": "2026-05-30T00:00:00Z",
         "exit_ts": "2026-05-31T00:00:00Z"},
    ]
    # ts inside [a] only
    m = match_open_decision(decisions, "SOLUSDT", "2026-06-02T00:00:00Z")
    assert m["id"] == "a"
    # ts inside no SOL interval
    assert match_open_decision(decisions, "SOLUSDT", "2026-06-10T00:00:00Z") is None
    # wrong symbol
    assert match_open_decision(decisions, "BTCUSDT", "2026-06-02T00:00:00Z") is None


def test_match_open_decision_falls_back_to_ts_when_opened_ts_none():
    # older records carry `ts` (decision time) but opened_ts=None (the cy22 SOL case)
    decisions = [{
        "id": "dc1b37", "symbol": "SOLUSDT", "opened_ts": None,
        "ts": "2026-06-02T08:27:00Z", "exit_ts": "2026-06-03T04:21:00Z",
    }]
    m = match_open_decision(decisions, "SOLUSDT", "2026-06-02T20:00:00Z")
    assert m is not None and m["id"] == "dc1b37"


def test_unrecorded_banks_flags_report_bank_absent_from_journal():
    decisions = [{
        "id": "dc1b37", "symbol": "SOLUSDT", "opened_ts": "2026-06-01T00:00:00Z",
        "exit_ts": "2026-06-03T04:00:00Z", "partial_banks": [],
    }]
    events = [{"symbol": "SOLUSDT", "cycle": 22, "pnl": 119.45, "fraction": 0.5,
               "ts": "2026-06-02T20:00:00Z"}]
    out = unrecorded_banks(decisions, events)
    assert len(out) == 1
    assert out[0]["decision_id"] == "dc1b37"
    assert out[0]["pnl"] == 119.45
    assert out[0]["cycle"] == 22


def test_unrecorded_banks_skips_already_recorded():
    decisions = [{
        "id": "dc1b37", "symbol": "SOLUSDT", "opened_ts": "2026-06-01T00:00:00Z",
        "exit_ts": "2026-06-03T04:00:00Z", "partial_banks": [{"pnl": 119.45, "cycle": 22}],
    }]
    events = [{"symbol": "SOLUSDT", "cycle": 22, "pnl": 119.45, "fraction": 0.5,
               "ts": "2026-06-02T20:00:00Z"}]
    assert unrecorded_banks(decisions, events) == []


def test_match_open_decision_handles_mixed_z_and_offset_ts_forms():
    # decision bounds in Z form, the report event ts in +00:00 offset form (the real cy22 mix) —
    # a naive lexical compare ('Z'=0x5A vs '+'=0x2B) could flip a boundary; parsing must not.
    decisions = [{"id": "a", "symbol": "SOLUSDT", "opened_ts": "2026-06-02T08:00:00Z",
                  "exit_ts": "2026-06-03T04:00:00Z"}]
    m = match_open_decision(decisions, "SOLUSDT", "2026-06-02T20:00:00+00:00")
    assert m is not None and m["id"] == "a"


def test_unrecorded_banks_dedup_distinguishes_two_same_cycle_banks():
    # two distinct reduces in the SAME cycle: recording one must NOT mask the other (cycle-only
    # dedup would wrongly skip the second). Key on (cycle, pnl).
    decisions = [{
        "id": "x", "symbol": "SOLUSDT", "opened_ts": "2026-06-01T00:00:00Z",
        "exit_ts": "2026-06-03T04:00:00Z", "partial_banks": [{"pnl": 119.45, "cycle": 22}],
    }]
    events = [
        {"symbol": "SOLUSDT", "cycle": 22, "pnl": 119.45, "fraction": 0.5,
         "ts": "2026-06-02T20:00:00Z"},   # already recorded
        {"symbol": "SOLUSDT", "cycle": 22, "pnl": 40.0, "fraction": 0.25,
         "ts": "2026-06-02T20:00:00Z"},   # distinct same-cycle bank, still unrecorded
    ]
    out = unrecorded_banks(decisions, events)
    assert [b["pnl"] for b in out] == [40.0]

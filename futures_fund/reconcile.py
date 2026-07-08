"""Balance-vs-journal reconciliation + scale-out backfill detection (non-protected instrumentation).

The deterministic account `balance` is the source of truth; it changes ONLY via the four executor
paths: `+= realized_pnl` on a full close, `+= realized_pnl` on a reduce/scale-out bank, and
`-= entry_fee` on open. So:

    balance == seed - Σ entry_fee + Σ realized_pnl(final closes) + Σ scale_out_banks

The journal's realized view (Σ realized_pnl) omits BOTH the entry fees (booked to balance at open,
never folded into realized_pnl which only nets the EXIT fee + funding) AND any scale-out bank whose
`partial_banks` slice was never written (the cy22 SOL +$119.45 predates the cy78 partial_banks fix).
`reconcile_balance` re-derives the expected balance from those components and surfaces the residual;
`unrecorded_banks` finds report-confirmed reduce banks missing from the journal so the CLI can
backfill them through the normal `append_partial_bank` path (never a hand-edit of state).
"""
from __future__ import annotations

from futures_fund.costs import trade_fee


def entry_fee_for(record: dict, *, pay_bnb: bool = False) -> float:
    """The taker entry fee balance paid at open for this record (qty * entry notional).

    The journal does not store entry_fee, so we re-derive it deterministically from the recorded
    size/entry — identical to what `executor.open_position` charged at open. Accepts either a closed
    decision (`size`) or an open Position (`qty`); both price identically off the entry notional."""
    qty = record.get("size") or record.get("qty")
    entry = record.get("entry")
    if not qty or not entry:
        return 0.0
    return trade_fee(qty * entry, maker=False, pay_bnb=pay_bnb)


def bank_total(decision: dict) -> float:
    """Sum of the decision's recorded scale-out bank PnL (the `partial_banks` slices)."""
    total = 0.0
    for b in (decision.get("partial_banks") or []):
        try:
            total += float(b.get("pnl") or 0.0)
        except (TypeError, ValueError):
            continue
    return total


def original_opened_qty(position: dict, report_events: list[dict] | None = None) -> float:
    """The position's ORIGINAL opened qty, reconstructed from its current (possibly reduced) qty and
    the reduce fractions applied to it. balance was debited the entry fee on the ORIGINAL qty at
    open and a partial reduce does NOT refund it, so pricing the open entry fee off the reduced
    runner qty under-counts it (the cy198 DOGE -$0.70 residual). Each reduce of fraction f leaves
    (1-f) of the qty, so original = current / Π(1-f) over the position's own reduce events (same
    symbol, cycle >= opened_cycle). No events / never-reduced -> keep==1 -> original == current."""
    cur = position.get("size") or position.get("qty")
    if not cur:
        return cur or 0.0
    sym = position.get("symbol")
    oc = position.get("opened_cycle")
    keep = 1.0
    for ev in (report_events or []):
        if ev.get("symbol") == sym and (oc is None or (ev.get("cycle") or -1) >= oc):
            f = ev.get("fraction")
            if f is not None and 0.0 < f < 1.0:
                keep *= (1.0 - f)
    return cur / keep if keep > 0 else cur


def open_position_banks(open_positions: list[dict], report_events: list[dict]) -> float:
    """Reduce/scale-out banks credited to `balance` for CURRENTLY-OPEN positions (a reduced runner
    that has not fully closed). Its `partial_banks` are NOT yet in the closed-trade journal, so the
    closed-only `scale_out_banks` sum misses them; sum the report reduce events belonging to each
    open position (same symbol, cycle >= its `opened_cycle`) so a book holding a reduced runner
    reconciles clean. They migrate into `scale_out_banks` when the runner finally closes (counted
    once, never double)."""
    total = 0.0
    for p in (open_positions or []):
        sym = p.get("symbol")
        oc = p.get("opened_cycle")
        for ev in report_events:
            if ev.get("symbol") == sym and (oc is None or (ev.get("cycle") or -1) >= oc):
                total += ev.get("pnl") or 0.0
    return total


def reconcile_balance(
    decisions: list[dict],
    balance: float,
    *,
    seed: float = 10000.0,
    pay_bnb: bool = False,
    open_positions: list[dict] | None = None,
    open_position_banks: float = 0.0,
    report_events: list[dict] | None = None,
) -> dict:
    """Re-derive the expected account balance and return the full attribution + residual. A non-zero
    residual means the journal does not fully explain balance (e.g. an unrecorded scale-out bank —
    see `unrecorded_banks`).

    `balance` is debited each OPEN position's entry fee at open AND credited any reduce-bank from a
    still-open reduced runner — neither is in the closed-trade journal until the position closes.
    Passing the live `open_positions` (entry fees) and `open_position_banks` (runner scale-outs,
    from `open_position_banks(...)`) folds both into `expected` so the residual reflects only
    genuinely-unexplained money — otherwise every cycle holding an open position (or a reduced
    runner) shows a spurious residual and trips the WARNING, eroding its power to flag a REAL
    divergence."""
    closed = [d for d in decisions if d.get("realized_pnl") is not None]
    realized_final = sum((d.get("realized_pnl") or 0.0) for d in closed)
    scale_out_banks = sum(bank_total(d) for d in closed)
    entry_fees = sum(entry_fee_for(d, pay_bnb=pay_bnb) for d in closed)
    # Price each open position's entry fee on its ORIGINAL opened qty — balance paid it in full at
    # open, so a reduced runner priced off its current qty under-counts it (the reduced fraction's
    # entry fee is orphaned: not in open_entry_fees, not in the closed journal). report_events carry
    # the reduce fractions used to reconstruct the original qty; absent them we use current qty.
    open_entry_fees = sum(
        entry_fee_for({"qty": original_opened_qty(p, report_events), "entry": p.get("entry")},
                      pay_bnb=pay_bnb)
        for p in (open_positions or [])
    )
    expected = (seed + realized_final + scale_out_banks - entry_fees
                - open_entry_fees + open_position_banks)
    return {
        "seed": seed,
        "realized_final": realized_final,
        "scale_out_banks": scale_out_banks,
        "entry_fees": entry_fees,
        "open_entry_fees": open_entry_fees,
        "open_position_banks": open_position_banks,
        "expected_balance": expected,
        "actual_balance": balance,
        "residual": balance - expected,
        "n_closed": len(closed),
    }


def match_open_decision(decisions: list[dict], symbol: str, ts: str) -> dict | None:
    """The closed decision for `symbol` whose [open, exit_ts] interval contains `ts` (an ISO-8601
    string; lexical compare is correct for zero-padded UTC ISO timestamps). The open bound is
    `opened_ts` when present, else the decision `ts` (Phase-1 records carry `ts`, and `opened_ts`
    can be None on older records). Attributes a report-confirmed reduce event to its trade."""
    for d in decisions:
        if d.get("symbol") != symbol:
            continue
        o = d.get("opened_ts") or d.get("ts")
        x = d.get("exit_ts")
        if o and x and o <= ts <= x:
            return d
    return None


def unrecorded_banks(decisions: list[dict], report_events: list[dict]) -> list[dict]:
    """Report-confirmed reduce/scale-out banks that are NOT reflected in the parent decision's
    `partial_banks` (idempotent by cycle). Each item carries the parent `decision_id` + the bank
    payload ready for `append_partial_bank`."""
    out: list[dict] = []
    for ev in report_events:
        d = match_open_decision(decisions, ev.get("symbol"), ev.get("ts") or "")
        if d is None:
            continue
        cycle = ev.get("cycle")
        # Dedup on (cycle, pnl) — keying on cycle alone would mask a SECOND distinct reduce in the
        # same cycle. pnl is matched within a cent (the bank stores full-precision realized pnl).
        already = any(
            b.get("cycle") == cycle and abs((b.get("pnl") or 0.0) - (ev.get("pnl") or 0.0)) < 0.01
            for b in (d.get("partial_banks") or [])
        )
        if already:
            continue
        out.append({
            "decision_id": d.get("id"),
            "symbol": ev.get("symbol"),
            "cycle": cycle,
            "pnl": ev.get("pnl"),
            "fraction": ev.get("fraction"),
            "ts": ev.get("ts"),
        })
    return out

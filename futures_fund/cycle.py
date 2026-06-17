from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from futures_fund.baseline import propose, simple_regime, swing_levels
from futures_fund.brief import last_completed_frame
from futures_fund.config import Settings
from futures_fund.consolidation import (
    cluster_scale,
    consolidate,
    cvar_risk_multiplier,
    position_risk,
)
from futures_fund.costs import count_funding_events
from futures_fund.executor import close_at_mark, open_position, reconcile
from futures_fund.exits import detect_exit
from futures_fund.hitrate import hit_rate, record_outcome
from futures_fund.journal import append_decision, patch_outcome, read_all_decisions, realized_total
from futures_fund.liquidation import liquidation_price, mmr_for_notional
from futures_fund.memory_layout import ensure_memory_layout
from futures_fund.models import TradeProposal
from futures_fund.policy import caps_for, circuit_breaker
from futures_fund.portfolio import portfolio_health
from futures_fund.profit_lock import ratcheted_stop
from futures_fund.replay import gap_window
from futures_fund.risk_gate import GateInputs, evaluate
from futures_fund.rr_floor import effective_rr_floor, load_rr_floor
from futures_fund.state import (
    AccountState,
    Position,
    is_halted,
    load_account,
    load_positions,
    save_account,
    save_positions,
)

_BASELINE = "baseline"
_SLIPPAGE_BPS = 2.0


@dataclass
class CycleContext:
    settings: Settings
    frames: dict
    fundings: dict
    specs: dict             # unified symbol -> SymbolSpec
    raw_to_unified: dict    # raw id -> unified symbol
    specs_by_raw: dict      # raw id -> SymbolSpec
    prices: dict            # raw id -> last close


def fetch_context(exchange, settings: Settings) -> CycleContext:
    """Fetch all per-symbol market data once and build the lookup maps the cycle needs."""
    frames = {s: exchange.ohlcv(s, settings.timeframe) for s in settings.symbols}
    fundings = {s: exchange.funding(s) for s in settings.symbols}
    specs = {s: exchange.symbol_spec(s) for s in settings.symbols}
    raw_to_unified = {specs[s].symbol: s for s in settings.symbols}
    specs_by_raw = {specs[s].symbol: specs[s] for s in settings.symbols}
    prices = {specs[s].symbol: float(frames[s]["close"].iloc[-1]) for s in settings.symbols}
    return CycleContext(settings, frames, fundings, specs, raw_to_unified, specs_by_raw, prices)


def _recent_returns(memory_dir, equity: float) -> list[float]:
    # TRUE realized (final close + partial banks) so a scaled-out trade isn't undercounted (cy78
    # review). Feeds cvar_risk_multiplier, which keys off the NEGATIVE tail, so a more-accurate
    # (less-undercounted) winner can only DE-RISK or leave unchanged — weakens no limit.
    pnls = [realized_total(d) for d in read_all_decisions(memory_dir)
            if d.get("realized_pnl") is not None]
    return [p / equity for p in pnls[-30:]] if equity > 0 else []


def _effective_funding_rate(entry_rate, exit_rate) -> float:
    """Minimum-viable per-hold funding rate (cy78 retrospective fix): the AVERAGE of the rate at
    ENTRY and at EXIT, instead of booking the exit-cycle rate over the WHOLE hold — which sign-
    INVERTED the carry on any trade that spanned a funding flip (a short that entered COLLECTING on
    negative funding then exited PAYING on positive funding was booked as paying the entire time,
    corrupting the exact carry signal the desk's flagship short edge harvests). None entry_rate ->
    fall back to the exit rate (legacy behavior)."""
    if entry_rate is None:
        return float(exit_rate)
    try:
        return (float(entry_rate) + float(exit_rate)) / 2.0
    except (TypeError, ValueError):
        return float(exit_rate)


def _entry_funding_rate(memory_dir, decision_id):
    """The funding rate recorded at entry (funding_at_entry) for `decision_id`, else None."""
    if not decision_id:
        return None
    try:
        for d in read_all_decisions(memory_dir):
            if d.get("id") == decision_id:
                return d.get("funding_at_entry")
    except Exception:  # noqa: BLE001 — never break the exit path over a journal read
        pass
    return None


def audit_and_reflect(ctx: CycleContext, positions: list[Position], account: AccountState,
                      memory_dir, now: datetime, report: dict,
                      agent_key: str = _BASELINE,
                      last_served_ts: datetime | None = None) -> list[Position]:
    """Phase 1: close positions whose missed-candle gap window hit stop/tp/liq; patch outcomes +
    hit-rate. `last_served_ts` (the prior cycle's served candle) is the floor of the gap window: the
    exit check considers every COMPLETED candle since it, not just the latest, so a stop/TP/liq
    touched during a candle the gate MISSED in an outage is honored. None (the default / cold start)
    -> single latest bar = today's behavior."""
    still_open: list[Position] = []
    for p in positions:
        sym = ctx.raw_to_unified.get(p.symbol)
        if sym is None:
            still_open.append(p)
            report["carried"] += 1
            continue
        # Exits read every COMPLETED candle since the LAST-SERVED candle, collapsed into one
        # conservative (max_high, min_low, first_open) gap window (futures_fund.replay). On the
        # normal +4h cadence this IS the single latest completed bar — byte-identical to the cy77
        # single-bar fix (the OHLCV feed's iloc[-1] is the still-FORMING candle, dropped inside
        # gap_window). After a loop outage it ALSO honors a stop/TP/liq touched during a MISSED
        # candle that price then recovered from — which a single-bar check silently dropped. The
        # window can only WIDEN [low, high]; it surfaces a missed exit, never suppresses one. The
        # gap-honest fill stays pessimistic: gap_open is the directionally-worst open (min for a
        # long, max for a short), so a stop gapped by a LATER missed bar fills at that worse open.
        win = gap_window(ctx.frames[sym], last_served_ts, now, ctx.settings.timeframe,
                         direction=p.direction)
        if win is None:
            still_open.append(p)
            continue
        g_high, g_low, g_open = win
        fr = ctx.fundings[sym]
        n_events = count_funding_events(p.opened_ts, now, int(fr.interval_hours))
        # Accrue funding at the AVERAGE of the entry and exit rates over the hold, not the exit rate
        # alone — the cy78 sign-inversion fix (see _effective_funding_rate).
        eff_rate = _effective_funding_rate(_entry_funding_rate(memory_dir, p.decision_id),
                                           fr.current_rate)
        ct = detect_exit(p, bar_high=g_high, bar_low=g_low,
                         bar_open=g_open,  # gap-honest fills (first missed bar's open)
                         funding_rate=eff_rate, funding_events=n_events,
                         slippage_bps=_SLIPPAGE_BPS)
        if ct is None:
            still_open.append(p)
            continue
        account.balance += ct.realized_pnl
        report["closed"] += 1
        report["actions"].append({"close": p.symbol, "reason": ct.reason, "pnl": ct.realized_pnl})
        if p.decision_id:
            patch_outcome(memory_dir, p.decision_id, {
                "exit_ts": now, "realized_pnl": ct.realized_pnl, "fees": ct.exit_fee,
                "funding_paid": ct.funding, "slippage": ct.slippage,
                "prediction_correct": ct.realized_pnl > 0,
            })
            record_outcome(memory_dir, agent_key, ct.realized_pnl > 0)
    return still_open


def _returns_corr(frames, raw_to_unified) -> dict:
    """Pairwise log-return correlation keyed by RAW symbol, from the cycle's OHLCV frames —
    feeds the correlated-as-one cluster cap. Missing pairs default to 0 (uncorrelated)."""
    import numpy as np
    series: dict = {}
    for raw, uni in raw_to_unified.items():
        df = frames.get(uni)
        if df is not None and len(df) > 6:
            series[raw] = np.diff(np.log(df["close"].to_numpy(dtype=float)))
    out: dict = {}
    syms = list(series)
    for i in range(len(syms)):
        for j in range(i + 1, len(syms)):
            a, b = syms[i], syms[j]
            m = min(len(series[a]), len(series[b]))
            if m > 6:
                r = float(np.corrcoef(series[a][-m:], series[b][-m:])[0, 1])
                if r == r:  # exclude NaN (flat series)
                    out[(a, b)] = r
    return out


def _derive_setup(quadrant: str | None, direction: str) -> str:
    """Coarse, deterministic setup archetype from the symbol's regime quadrant (cy78 fix so the
    learning grid has a `setup` axis instead of all-null). trend->trend_follow, range->mean_rev,
    transition->transition; the Trader may later carry an explicit finer label."""
    q = quadrant or ""
    if "trend" in q:
        return "trend_follow"
    if "range" in q:
        return "mean_reversion"
    if "transition" in q:
        return "transition"
    return "unclassified"


def _retrieved_lesson_ids(state_dir, cycle_no: int) -> list[str]:
    """The lesson ids injected into THIS cycle's debate (from state/cycle/N/lessons.json), stamped
    onto each decision as `retrieved_memory_ids` — the Direction-B prerequisite so a lesson's
    confirm/demote can be driven from the outcomes of the trades it informed. Fail-safe -> []."""
    try:
        from futures_fund.cycle_io import load_output
        payload = load_output("state", cycle_no, "lessons")
        return [lz.get("id") for lz in (payload.get("lessons") or []) if lz.get("id")]
    except Exception:  # noqa: BLE001 — never break the cycle over an absent/garbled lessons file
        return []


def execute_proposals(  # noqa: PLR0912
        ctx: CycleContext, proposals: list[TradeProposal], contributing_agents: list[str],
        positions: list[Position], account: AccountState, state_dir, memory_dir,
        now: datetime, cycle_no: int, report: dict | None = None,
        agent_key: str = _BASELINE, rationale_by_symbol: dict | None = None,
        close_absent: bool = True, force_close: set[str] | None = None,
        prediction_by_symbol: dict | None = None) -> dict:
    """Phases 7-10 for a given set of trade proposals (from the baseline OR the agent team):
    risk-gate each proposal, consolidate to a book, reconcile/execute, journal, persist.
    Reusable by both the baseline cycle and the Phase-B agent cycle.

    Holdings-review parameters (agent path):
    - close_absent=True (baseline): a held position absent from the new target book is closed by
      reconciliation. close_absent=False (agent path with an explicit holdings review): a holding
      is closed ONLY when named in `force_close` — universe rotation never churns it, and the
      gross heat of the kept holdings is reserved from the new-opens budget."""
    if report is None:
        report = {"cycle": cycle_no, "halted": False, "opened": 0, "closed": 0,
                  "carried": 0, "stuck_close": 0, "equity": account.balance, "actions": []}
    if not ctx.settings.symbols:
        # Empty universe (failed scan / degenerate Watcher output) -> stand down, never trade.
        report["stood_down"] = True
        return report
    health = portfolio_health(account.balance, account.peak_equity, positions, ctx.prices,
                              recent_hit_rate=hit_rate(memory_dir, agent_key))
    # symbols[0] is the market bellwether (convention: BTC first) for the portfolio heat cap;
    # per-proposal gating below still uses each symbol's own regime.
    caps = caps_for(simple_regime(ctx.frames[ctx.settings.symbols[0]]), health)
    open_dicts = [{"symbol": p.symbol, "direction": p.direction, "qty": p.qty,
                   "entry": p.entry, "stop": p.stop} for p in positions]

    from futures_fund.equity_log import period_return
    daily_pnl = period_return(state_dir, now, 1)
    weekly_pnl = period_return(state_dir, now, 7)
    monthly_pnl = period_return(state_dir, now, 30)

    approved = []
    vetoed: list = []
    floor_state = load_rr_floor(state_dir)   # per-quadrant adaptive RR floor (seed 2.0 = legacy)
    for prop in proposals:
        spec = ctx.specs_by_raw.get(prop.symbol)
        if spec is None:
            continue
        unified = ctx.raw_to_unified[prop.symbol]
        regime = simple_regime(ctx.frames[unified])   # GateInputs.regime (sizing/caps) — unchanged
        # The RR-FLOOR quadrant must use the COMPLETED frame the trigger fires on, matching CP9's
        # arm-time quadrant, so an adapted per-quadrant floor can't arm a trigger then RR-veto it.
        _cdf = last_completed_frame(ctx.frames[unified], now, ctx.settings.timeframe)
        floor_quadrant = (simple_regime(_cdf).quadrant
                          if _cdf is not None and len(_cdf) else regime.quadrant)
        rr_floor = effective_rr_floor(floor_quadrant, floor_state)
        # Structural S/R for the open-air-TP guard, computed from the bars BEFORE the latest
        # completed (firing) bar: a breakout/trigger-fill PRINTS its own new extreme on the firing
        # bar, so including it would mistake that just-made high/low for a forward obstacle and veto
        # a legitimate break. Prior structure is the resistance/support the trade actually faces.
        _sh, _sl = (swing_levels(_cdf.iloc[:-1]) if _cdf is not None and len(_cdf) > 1
                    else (None, None))
        decision = evaluate(GateInputs(proposal=prop, spec=spec, regime=regime,
                                       health=health, open_positions=open_dicts,
                                       daily_pnl_pct=daily_pnl, weekly_pnl_pct=weekly_pnl,
                                       monthly_pnl_pct=monthly_pnl, rr_floor=rr_floor,
                                       swing_high=_sh, swing_low=_sl))
        if decision.verdict in ("approve", "resize") and decision.sized_trade is not None:
            approved.append(decision.sized_trade)
        else:
            vetoed.append({"symbol": prop.symbol, "direction": prop.direction,
                           "entry": prop.entry, "stop": prop.stop,
                           "take_profits": prop.take_profits, "reason": decision.reason,
                           "quadrant": floor_quadrant,   # the quadrant the RR floor was judged on
                           "id": f"{cycle_no}:{prop.symbol}:{prop.direction}"})

    cvar_mult = cvar_risk_multiplier(_recent_returns(memory_dir, health.equity))
    force_close = set(force_close or set())
    # Hard circuit breaker: a -12% month (or its peers) FLATTENS the entire book — close every
    # holding at mark this cycle, regardless of the review's per-position verdicts.
    breaker = circuit_breaker(daily_pnl, weekly_pnl, monthly_pnl, health.drawdown_from_peak)
    if breaker.force_flatten:
        force_close |= {p.symbol for p in positions}
        report["force_flatten"] = breaker.reason
    # A force_close position is only genuinely closeable if priceable; otherwise it stays open
    # (stuck) and its heat must still be reserved so the gross-heat cap binds on the REAL book.
    closeable = {p.symbol for p in positions if p.symbol in force_close
                 and ctx.raw_to_unified.get(p.symbol) is not None and p.symbol in ctx.prices}
    # Reserve gross heat for every carried position that SURVIVES this cycle (kept holdings +
    # any stuck force-close) so new opens get only the remaining headroom under the cap.
    reserved = 0.0 if close_absent else sum(
        position_risk(p.qty, p.entry, p.stop, health.equity, p.direction)
        for p in positions if p.symbol not in closeable)
    book = consolidate(approved, health.equity, max(0.0, caps.max_heat - reserved),
                       cvar_mult=cvar_mult)

    target = {st.proposal.symbol: st for st in book}
    if close_absent:
        to_open, reconcile_close = reconcile(target, positions)  # baseline: absence/flip closes
        to_close = list(reconcile_close)
        to_close += [p for p in positions if p.symbol in force_close and p not in to_close]
    else:
        # Explicit holdings review: NEVER re-open or flip a KEPT holding (a HOLD stays as-is);
        # force-closed symbols are not kept, so a re-proposal on them is a legitimate fresh open.
        kept = {p.symbol for p in positions if p.symbol not in closeable}
        to_open = [st for st in book if st.proposal.symbol not in kept]
        to_close = [p for p in positions if p.symbol in closeable]
    closed_syms: set[str] = set()
    for p in to_close:
        sym = ctx.raw_to_unified.get(p.symbol)
        if sym is None or p.symbol not in ctx.prices:
            continue
        fr = ctx.fundings[sym]
        n_events = count_funding_events(p.opened_ts, now, int(fr.interval_hours))
        # Effective (entry+exit average) funding rate — the cy78 sign fix, applied to the RM-CLOSE/
        # reconcile path too (not just the stop/TP/liq path) so all three close paths agree.
        eff_rate = _effective_funding_rate(_entry_funding_rate(memory_dir, p.decision_id),
                                           fr.current_rate)
        ct = close_at_mark(p, ctx.prices[p.symbol], funding_rate=eff_rate,
                           funding_events=n_events, slippage_bps=_SLIPPAGE_BPS)
        account.balance += ct.realized_pnl
        report["closed"] += 1
        closed_syms.add(p.symbol)
        reason = "holdings_close" if p.symbol in force_close else "reconcile"
        report["actions"].append({"close": p.symbol, "reason": reason, "pnl": ct.realized_pnl})
        if p.decision_id:
            patch_outcome(memory_dir, p.decision_id, {
                "exit_ts": now, "realized_pnl": ct.realized_pnl, "fees": ct.exit_fee,
                "funding_paid": ct.funding, "prediction_correct": ct.realized_pnl > 0,
            })
            record_outcome(memory_dir, agent_key, ct.realized_pnl > 0)
    keep = [p for p in positions if p.symbol not in closed_syms]
    # reconcile wanted these closed but they were unpriceable -> stuck open (not a voluntary carry)
    report["stuck_close"] += sum(1 for p in to_close if p.symbol not in closed_syms)
    # report ACTUAL holdings-review closes (not intent), and any force-close we could NOT execute
    report["closed_by_review"] = len(force_close & closed_syms)
    stranded = sorted(force_close - closed_syms)
    if stranded:
        report["stranded"] = stranded  # e.g. delisted/unpriceable holdings an operator must flatten

    # Correlated-as-one: never let correlated same-direction bets (held + new) pile into one
    # oversized directional position. A correlated cluster may use at most ~half the heat budget.
    cluster_cap = max(caps.per_trade_risk_pct, 0.5 * caps.max_heat)
    held_dicts = [{"symbol": p.symbol, "direction": p.direction, "qty": p.qty,
                   "entry": p.entry, "stop": p.stop} for p in keep]
    _before = len(to_open)
    to_open = cluster_scale(to_open, held_dicts, health.equity,
                            _returns_corr(ctx.frames, ctx.raw_to_unified), cluster_cap)
    if len(to_open) < _before:
        report["cluster_trimmed"] = _before - len(to_open)

    retrieved_ids = _retrieved_lesson_ids(state_dir, cycle_no)  # for retrieved_memory_ids stamping
    for st in to_open:
        spec = ctx.specs_by_raw[st.proposal.symbol]
        # cy78 journal-hygiene: stamp regime/setup/retrieved_memory_ids at decision-time so the
        # all-weather learning grid has cells and Direction-B lesson confirm/demote can be driven.
        uni = ctx.raw_to_unified.get(st.proposal.symbol)
        quad = simple_regime(ctx.frames[uni]).quadrant if uni in ctx.frames else None
        did = append_decision(memory_dir, {
            "ts": now, "cycle": cycle_no, "symbol": st.proposal.symbol,
            "direction": st.proposal.direction, "entry": st.proposal.entry,
            "stop": st.proposal.stop, "take_profit": st.proposal.take_profits, "size": st.qty,
            "leverage": st.leverage, "funding_at_entry": st.proposal.funding_rate,
            "confidence": st.proposal.confidence, "dominant_signal": contributing_agents[0]
            if contributing_agents else "unknown", "contributing_agents": contributing_agents,
            "rationale": (rationale_by_symbol or {}).get(st.proposal.symbol),
            "falsifiable_prediction": (prediction_by_symbol or {}).get(st.proposal.symbol),
            "regime": quad, "setup": _derive_setup(quad, st.proposal.direction),
            "retrieved_memory_ids": retrieved_ids,
        })
        pos, entry_fee = open_position(st, cycle_no, now, _SLIPPAGE_BPS, decision_id=did)
        mmr, maint = mmr_for_notional(pos.qty * pos.entry, spec.mmr_brackets)
        liq = liquidation_price(pos.entry, pos.qty, pos.margin, pos.direction, mmr, maint)
        pos = pos.model_copy(update={"liq_price": liq})
        # Fire-time profit-lock ladder (#268). A position opens AFTER the management stage, so the
        # RM cannot trail it until NEXT cycle. Ratchet its stop toward profit NOW from the firing
        # candle's favorable excursion (deterministic, no LLM), and record the ORIGINAL stop in
        # entry_stop. This protects a freshly-fired deep-in-profit position through the gap to the
        # next management cycle / an outage (the cy97 BTC +1.6R -> -1.12R round-trip). Carried
        # positions are NOT touched here — they are the RM's to manage. TIGHTEN-ONLY via
        # ratcheted_stop -> STRENGTHENS the safety path, never loosens.
        _usym = ctx.raw_to_unified.get(pos.symbol)
        _orig_stop, _ratchet = pos.stop, None
        if _usym is not None:
            _fb = last_completed_frame(ctx.frames[_usym], now, ctx.settings.timeframe).iloc[-1]
            _ratchet = ratcheted_stop(pos.direction, pos.entry, _orig_stop, _orig_stop,
                                      float(_fb["high"]), float(_fb["low"]), float(_fb["close"]))
        pos = pos.model_copy(update={"entry_stop": _orig_stop,
                                     "stop": _ratchet if _ratchet is not None else _orig_stop})
        if _ratchet is not None:
            report["profit_locks_ratcheted"] = report.get("profit_locks_ratcheted", 0) + 1
        account.balance -= entry_fee
        keep.append(pos)
        report["opened"] += 1
        report["actions"].append({"open": pos.symbol, "direction": pos.direction})

    final_health = portfolio_health(account.balance, account.peak_equity, keep, ctx.prices,
                                    recent_hit_rate=hit_rate(memory_dir, agent_key))
    account.peak_equity = max(account.peak_equity, final_health.equity)
    account.updated_ts = now
    save_account(state_dir, account)
    save_positions(state_dir, keep)
    report["equity"] = final_health.equity
    report.setdefault("vetoed", 0)
    from futures_fund.equity_log import record_equity
    record_equity(state_dir, now, final_health.equity, cycle_no)
    from futures_fund.shadow import record_shadow
    if vetoed:
        record_shadow(state_dir, now, cycle_no, vetoed)
        report["vetoed"] = len(vetoed)
    return report


def run_cycle(exchange, settings: Settings, state_dir, memory_dir,
              now: datetime, cycle_no: int) -> dict:
    """Run one deterministic baseline cycle (phases 0-11, no LLM). Returns a CycleReport dict."""
    ensure_memory_layout(memory_dir)
    account = load_account(state_dir, settings.account_size_usdt)
    positions = load_positions(state_dir)
    report = {"cycle": cycle_no, "halted": False, "opened": 0, "closed": 0,
              "carried": 0, "stuck_close": 0, "equity": account.balance, "actions": []}
    if is_halted(state_dir):
        report["halted"] = True
        return report

    ctx = fetch_context(exchange, settings)
    positions = audit_and_reflect(ctx, positions, account, memory_dir, now, report)

    proposals = []
    for s in settings.symbols:
        spec = ctx.specs[s]
        prop = propose(spec.symbol, ctx.frames[s], ctx.fundings[s].current_rate, horizon_hours=4.0)
        if prop is not None:
            proposals.append(prop)

    return execute_proposals(ctx, proposals, [_BASELINE], positions, account,
                             state_dir, memory_dir, now, cycle_no, report)

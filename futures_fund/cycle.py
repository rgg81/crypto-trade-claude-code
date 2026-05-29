from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from futures_fund.baseline import propose, simple_regime
from futures_fund.config import Settings
from futures_fund.consolidation import consolidate, cvar_risk_multiplier
from futures_fund.costs import count_funding_events
from futures_fund.executor import close_at_mark, open_position, reconcile
from futures_fund.exits import detect_exit
from futures_fund.hitrate import hit_rate, record_outcome
from futures_fund.journal import append_decision, patch_outcome, read_all_decisions
from futures_fund.liquidation import liquidation_price, mmr_for_notional
from futures_fund.memory_layout import ensure_memory_layout
from futures_fund.models import TradeProposal
from futures_fund.policy import caps_for
from futures_fund.portfolio import portfolio_health
from futures_fund.risk_gate import GateInputs, evaluate
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
    pnls = [d["realized_pnl"] for d in read_all_decisions(memory_dir)
            if d.get("realized_pnl") is not None]
    return [p / equity for p in pnls[-30:]] if equity > 0 else []


def audit_and_reflect(ctx: CycleContext, positions: list[Position], account: AccountState,
                      memory_dir, now: datetime, report: dict,
                      agent_key: str = _BASELINE) -> list[Position]:
    """Phase 1: close positions whose latest bar hit stop/tp/liq; patch outcomes + hit-rate."""
    still_open: list[Position] = []
    for p in positions:
        sym = ctx.raw_to_unified.get(p.symbol)
        if sym is None:
            still_open.append(p)
            report["carried"] += 1
            continue
        bar = ctx.frames[sym].iloc[-1]
        fr = ctx.fundings[sym]
        n_events = count_funding_events(p.opened_ts, now, int(fr.interval_hours))
        ct = detect_exit(p, bar_high=float(bar["high"]), bar_low=float(bar["low"]),
                         funding_rate=fr.current_rate, funding_events=n_events,
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


def execute_proposals(  # noqa: PLR0912
        ctx: CycleContext, proposals: list[TradeProposal], contributing_agents: list[str],
        positions: list[Position], account: AccountState, state_dir, memory_dir,
        now: datetime, cycle_no: int, report: dict | None = None,
        agent_key: str = _BASELINE, rationale_by_symbol: dict | None = None) -> dict:
    """Phases 7-10 for a given set of trade proposals (from the baseline OR the agent team):
    risk-gate each proposal, consolidate to a book, reconcile/execute, journal, persist.
    Reusable by both the baseline cycle and the Phase-B agent cycle."""
    if report is None:
        report = {"cycle": cycle_no, "halted": False, "opened": 0, "closed": 0,
                  "carried": 0, "stuck_close": 0, "equity": account.balance, "actions": []}
    health = portfolio_health(account.balance, account.peak_equity, positions, ctx.prices,
                              recent_hit_rate=hit_rate(memory_dir, agent_key))
    # symbols[0] is the market bellwether (convention: BTC first) for the portfolio heat cap;
    # per-proposal gating below still uses each symbol's own regime.
    caps = caps_for(simple_regime(ctx.frames[ctx.settings.symbols[0]]), health)
    open_dicts = [{"symbol": p.symbol, "direction": p.direction, "qty": p.qty,
                   "entry": p.entry, "stop": p.stop} for p in positions]

    approved = []
    vetoed: list = []
    for prop in proposals:
        spec = ctx.specs_by_raw.get(prop.symbol)
        if spec is None:
            continue
        unified = ctx.raw_to_unified[prop.symbol]
        decision = evaluate(GateInputs(proposal=prop, spec=spec,
                                       regime=simple_regime(ctx.frames[unified]),
                                       health=health, open_positions=open_dicts))
        if decision.verdict in ("approve", "resize") and decision.sized_trade is not None:
            approved.append(decision.sized_trade)
        else:
            vetoed.append({"symbol": prop.symbol, "direction": prop.direction,
                           "entry": prop.entry, "stop": prop.stop,
                           "take_profits": prop.take_profits, "reason": decision.reason})

    cvar_mult = cvar_risk_multiplier(_recent_returns(memory_dir, health.equity))
    book = consolidate(approved, health.equity, caps.max_heat, cvar_mult=cvar_mult)

    target = {st.proposal.symbol: st for st in book}
    to_open, to_close = reconcile(target, positions)
    closed_syms: set[str] = set()
    for p in to_close:
        sym = ctx.raw_to_unified.get(p.symbol)
        if sym is None or p.symbol not in ctx.prices:
            continue
        fr = ctx.fundings[sym]
        n_events = count_funding_events(p.opened_ts, now, int(fr.interval_hours))
        ct = close_at_mark(p, ctx.prices[p.symbol], funding_rate=fr.current_rate,
                           funding_events=n_events, slippage_bps=_SLIPPAGE_BPS)
        account.balance += ct.realized_pnl
        report["closed"] += 1
        closed_syms.add(p.symbol)
        report["actions"].append({"close": p.symbol, "reason": "close", "pnl": ct.realized_pnl})
        if p.decision_id:
            patch_outcome(memory_dir, p.decision_id, {
                "exit_ts": now, "realized_pnl": ct.realized_pnl, "fees": ct.exit_fee,
                "funding_paid": ct.funding, "prediction_correct": ct.realized_pnl > 0,
            })
            record_outcome(memory_dir, agent_key, ct.realized_pnl > 0)
    keep = [p for p in positions if p.symbol not in closed_syms]
    # reconcile wanted these closed but they were unpriceable -> stuck open (not a voluntary carry)
    report["stuck_close"] += sum(1 for p in to_close if p.symbol not in closed_syms)

    for st in to_open:
        spec = ctx.specs_by_raw[st.proposal.symbol]
        did = append_decision(memory_dir, {
            "ts": now, "cycle": cycle_no, "symbol": st.proposal.symbol,
            "direction": st.proposal.direction, "entry": st.proposal.entry,
            "stop": st.proposal.stop, "take_profit": st.proposal.take_profits, "size": st.qty,
            "leverage": st.leverage, "funding_at_entry": st.proposal.funding_rate,
            "confidence": st.proposal.confidence, "dominant_signal": contributing_agents[0]
            if contributing_agents else "unknown", "contributing_agents": contributing_agents,
            "rationale": (rationale_by_symbol or {}).get(st.proposal.symbol),
        })
        pos, entry_fee = open_position(st, cycle_no, now, _SLIPPAGE_BPS, decision_id=did)
        mmr, maint = mmr_for_notional(pos.qty * pos.entry, spec.mmr_brackets)
        liq = liquidation_price(pos.entry, pos.qty, pos.margin, pos.direction, mmr, maint)
        pos = pos.model_copy(update={"liq_price": liq})
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

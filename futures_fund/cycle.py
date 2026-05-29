from __future__ import annotations

from datetime import datetime

from futures_fund.baseline import propose, simple_regime
from futures_fund.config import Settings
from futures_fund.consolidation import consolidate, cvar_risk_multiplier
from futures_fund.executor import close_at_mark, open_position, reconcile
from futures_fund.exits import detect_exit
from futures_fund.hitrate import hit_rate, record_outcome
from futures_fund.journal import append_decision, patch_outcome, read_all_decisions
from futures_fund.memory_layout import ensure_memory_layout
from futures_fund.policy import caps_for
from futures_fund.portfolio import portfolio_health
from futures_fund.risk_gate import GateInputs, evaluate
from futures_fund.state import (
    Position,
    is_halted,
    load_account,
    load_positions,
    save_account,
    save_positions,
)

_BASELINE = "baseline"
_SLIPPAGE_BPS = 2.0


def _hours_held(opened_ts: datetime, now: datetime) -> float:
    return max(0.0, (now - opened_ts).total_seconds() / 3600.0)


def _recent_returns(memory_dir, equity: float) -> list[float]:
    pnls = [d["realized_pnl"] for d in read_all_decisions(memory_dir)
            if d.get("realized_pnl") is not None]
    return [p / equity for p in pnls[-30:]] if equity > 0 else []


def run_cycle(exchange, settings: Settings, state_dir, memory_dir,
              now: datetime, cycle_no: int) -> dict:
    """Run one deterministic trading cycle (phases 0-11, no LLM). Returns a CycleReport dict.
    `exchange` must expose symbol_spec/ohlcv/funding/mark_price (FuturesExchange or a fake)."""
    # Phase 0 — preflight
    ensure_memory_layout(memory_dir)
    account = load_account(state_dir, settings.account_size_usdt)
    positions = load_positions(state_dir)
    report = {"cycle": cycle_no, "halted": False, "opened": 0, "closed": 0,
              "equity": account.balance, "actions": []}
    if is_halted(state_dir):
        report["halted"] = True
        return report

    frames = {s: exchange.ohlcv(s, settings.timeframe) for s in settings.symbols}
    fundings = {s: exchange.funding(s) for s in settings.symbols}

    # Phase 1 — audit & reflect: close positions whose latest bar hit stop/tp/liq
    still_open: list[Position] = []
    for p in positions:
        sym = next(
            (s for s in settings.symbols if exchange.symbol_spec(s).symbol == p.symbol), None
        )
        df = frames.get(sym) if sym else None
        if df is None:
            still_open.append(p)
            continue
        bar = df.iloc[-1]
        fr = fundings[sym]
        n_events = int(_hours_held(p.opened_ts, now) // fr.interval_hours)
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
            record_outcome(memory_dir, _BASELINE, ct.realized_pnl > 0)
    positions = still_open

    # Phase 2 — regime + portfolio health
    prices = {
        exchange.symbol_spec(s).symbol: float(frames[s]["close"].iloc[-1])
        for s in settings.symbols
    }
    health = portfolio_health(account.balance, account.peak_equity, positions, prices,
                              recent_hit_rate=hit_rate(memory_dir, _BASELINE))
    btc_df = frames[settings.symbols[0]]
    caps = caps_for(simple_regime(btc_df), health)
    report["equity"] = health.equity

    # Phases 3-7 — watcher (configured symbols) -> baseline proposals -> risk gate
    open_dicts = [{"symbol": p.symbol, "direction": p.direction, "qty": p.qty,
                   "entry": p.entry, "stop": p.stop} for p in positions]
    approved = []
    for s in settings.symbols:
        spec = exchange.symbol_spec(s)
        prop = propose(spec.symbol, frames[s], fundings[s].current_rate,
                       horizon_hours=4.0)
        if prop is None:
            continue
        decision = evaluate(GateInputs(
            proposal=prop, spec=spec, regime=simple_regime(frames[s]), health=health,
            open_positions=open_dicts,
        ))
        if decision.verdict in ("approve", "resize") and decision.sized_trade is not None:
            approved.append(decision.sized_trade)

    # Phase 8 — consolidation (gross-heat cap + CVaR de-risk)
    cvar_mult = cvar_risk_multiplier(_recent_returns(memory_dir, health.equity))
    book = consolidate(approved, health.equity, caps.max_heat, cvar_mult=cvar_mult)

    # Phase 9 — execution (reconcile + fills + journal Phase-1)
    target = {st.proposal.symbol: st for st in book}
    to_open, to_close = reconcile(target, positions)
    for p in to_close:
        ct = close_at_mark(p, prices.get(p.symbol, p.entry), funding_rate=0.0,
                           funding_events=0, slippage_bps=_SLIPPAGE_BPS)
        account.balance += ct.realized_pnl
        report["closed"] += 1
        if p.decision_id:
            patch_outcome(memory_dir, p.decision_id, {
                "exit_ts": now, "realized_pnl": ct.realized_pnl, "fees": ct.exit_fee,
                "prediction_correct": ct.realized_pnl > 0,
            })
            record_outcome(memory_dir, _BASELINE, ct.realized_pnl > 0)
    keep = [p for p in positions if p not in to_close]
    for st in to_open:
        did = append_decision(memory_dir, {
            "ts": now, "cycle": cycle_no, "symbol": st.proposal.symbol,
            "direction": st.proposal.direction, "entry": st.proposal.entry,
            "stop": st.proposal.stop,
            "take_profit": st.proposal.take_profits, "size": st.qty, "leverage": st.leverage,
            "funding_at_entry": st.proposal.funding_rate, "confidence": st.proposal.confidence,
            "dominant_signal": "baseline-momentum", "contributing_agents": [_BASELINE],
        })
        pos, entry_fee = open_position(st, cycle_no, now, _SLIPPAGE_BPS, decision_id=did)
        account.balance -= entry_fee
        keep.append(pos)
        report["opened"] += 1
        report["actions"].append({"open": pos.symbol, "direction": pos.direction})
    positions = keep

    # Phase 10 — persist (no silent runs) + recompute equity/peak
    final_health = portfolio_health(account.balance, account.peak_equity, positions, prices,
                                    recent_hit_rate=hit_rate(memory_dir, _BASELINE))
    account.peak_equity = max(account.peak_equity, final_health.equity)
    account.updated_ts = now
    save_account(state_dir, account)
    save_positions(state_dir, positions)
    report["equity"] = final_health.equity
    return report

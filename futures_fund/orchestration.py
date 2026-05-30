from __future__ import annotations

from datetime import datetime

from futures_fund.brief import build_symbol_brief
from futures_fund.config import Settings
from futures_fund.contracts import to_trade_proposal
from futures_fund.cycle import audit_and_reflect, execute_proposals, fetch_context
from futures_fund.hitrate import hit_rate
from futures_fund.memory_layout import ensure_memory_layout
from futures_fund.portfolio import portfolio_health
from futures_fund.reflect import reflection_payload
from futures_fund.screen import screen_reports
from futures_fund.state import is_halted, load_account, load_positions, save_account, save_positions

_AGENT_KEY = "team"


def working_universe(exchange, settings: Settings, positions) -> Settings:
    """The universe analysed/gated this cycle = the configured symbols (the Watcher's fresh picks)
    PLUS every symbol we currently hold. Force-including holdings is what makes the dynamic
    universe safe: a carried position whose symbol is no longer a top mover is still audited,
    re-analysed (HOLD vs CLOSE), priced, and reconciled — never stranded by rotation."""
    syms = list(settings.symbols)
    seen = set(syms)
    unify = getattr(exchange, "unified_for_raw", None)
    for p in positions:
        u = unify(p.symbol) if unify else None
        if u and u not in seen:
            syms.append(u)
            seen.add(u)
    return settings.model_copy(update={"symbols": syms}) if syms != list(settings.symbols) \
        else settings


_TF_HOURS = {"15m": 0.25, "1h": 1.0, "4h": 4.0, "1d": 24.0}


def _holding_card(pos, brief: dict, now: datetime, timeframe: str, decision: dict | None) -> dict:
    """The 'position card' the team reads to decide HOLD vs CLOSE on a carried position:
    current mark, unrealized PnL, progress in R toward target/stop, time held, distance to
    stop/liquidation, and the ORIGINAL thesis + falsifiable prediction it was opened on."""
    mark = float(brief.get("mark_price") or brief.get("last_close"))
    sign = 1.0 if pos.direction == "long" else -1.0
    risk_per_unit = abs(pos.entry - pos.stop) or 1e-9
    tf = _TF_HOURS.get(timeframe, 4.0)
    bars_held = (now - pos.opened_ts).total_seconds() / 3600.0 / tf
    card = {
        "direction": pos.direction, "qty": pos.qty, "entry": pos.entry, "stop": pos.stop,
        "take_profits": pos.take_profits, "mark": mark, "liq_price": pos.liq_price,
        "unrealized_pnl_pct": round(sign * (mark - pos.entry) / pos.entry, 4),
        "r_progress": round(sign * (mark - pos.entry) / risk_per_unit, 2),
        "dist_to_stop_pct": round(abs(mark - pos.stop) / mark, 4) if mark else None,
        "dist_to_liq_pct": round(abs(pos.liq_price - mark) / mark, 4) if mark else None,
        "bars_held": round(bars_held, 1), "opened_cycle": pos.opened_cycle,
        "decision_id": pos.decision_id,
    }
    if decision:
        card["original_thesis"] = decision.get("rationale") or decision.get("thesis")
        card["falsifiable_prediction"] = decision.get("falsifiable_prediction")
        card["confidence_at_entry"] = decision.get("confidence")
    return card


def preflight_step(exchange, settings: Settings, state_dir, memory_dir,
                   now: datetime, cycle_no: int, http_client=None) -> dict:
    """Phase 0-2: load state, audit exits (BEFORE the halt check so a halt still closes
    stop/tp/liq hits), then build the per-symbol briefs + health/regime for the analysts."""
    ensure_memory_layout(memory_dir)
    import os

    from futures_fund.market_context import build_market_context
    _owns_client = http_client is None
    if _owns_client:
        import httpx
        http_client = httpx.Client(timeout=15.0)
    try:
        market_context = build_market_context(http_client, settings,
                                              fred_key=os.environ.get(settings.data.fred_key_env))
    finally:
        if _owns_client:
            http_client.close()  # don't leak a client per cycle
    account = load_account(state_dir, settings.account_size_usdt)
    positions = load_positions(state_dir)
    settings = working_universe(exchange, settings, positions)  # carry held symbols in
    report = {"cycle": cycle_no, "halted": False, "opened": 0, "closed": 0,
              "carried": 0, "stuck_close": 0, "equity": account.balance, "actions": []}
    ctx = fetch_context(exchange, settings)
    positions = audit_and_reflect(ctx, positions, account, memory_dir, now, report,
                                  agent_key=_AGENT_KEY)
    save_account(state_dir, account)
    save_positions(state_dir, positions)
    from futures_fund.scorecard import build_scorecard
    if is_halted(state_dir):
        return {"cycle": cycle_no, "halted": True, "briefs": [], "equity": account.balance,
                "open_positions": [{"symbol": p.symbol, "direction": p.direction}
                                   for p in positions],
                "audit": {"closed": report["closed"], "carried": report["carried"]},
                "market_context": market_context,
                "scorecard": build_scorecard(state_dir, memory_dir)}
    health = portfolio_health(account.balance, account.peak_equity, positions, ctx.prices,
                              recent_hit_rate=hit_rate(memory_dir, _AGENT_KEY))
    scorecard = build_scorecard(state_dir, memory_dir, monthly_target=0.05)
    from futures_fund.journal import read_open_decisions
    held_by_raw = {p.symbol: p for p in positions}
    decisions_by_id = {d.get("id"): d for d in read_open_decisions(memory_dir)}
    briefs = []
    for s in settings.symbols:
        b = build_symbol_brief(exchange, s, settings.timeframe)
        b["exchange_id"] = ctx.specs[s].symbol  # raw id (e.g. BTCUSDT) agents MUST use for output
        pos = held_by_raw.get(b["exchange_id"])
        if pos is not None:  # carried position -> attach the HOLD/CLOSE review card
            b["holding"] = _holding_card(pos, b, now, settings.timeframe,
                                         decisions_by_id.get(pos.decision_id))
        briefs.append(b)
    try:
        from futures_fund.vendors import archive_jsonl
        for b in briefs:
            rec = {"ts": now.isoformat(), "symbol": b["exchange_id"],
                   "oi_value": b.get("oi_value"), "long_short_ratio": b.get("long_short_ratio")}
            archive_jsonl(f"{settings.data.archive_dir}/derivatives.jsonl", [rec], key="ts")
    except Exception:
        pass  # graceful: archiving must never break the cycle
    return {
        "cycle": cycle_no, "halted": False, "equity": health.equity,
        "drawdown_from_peak": health.drawdown_from_peak, "health_tier": health.tier,
        "briefs": briefs,
        "open_positions": [{"symbol": p.symbol, "direction": p.direction, "qty": p.qty,
                            "entry": p.entry} for p in positions],
        "audit": {"closed": report["closed"], "carried": report["carried"]},
        "market_context": market_context,
        "scorecard": scorecard,
    }


def screen_step(reports, top_n: int = 5) -> list[str]:
    """Phase 4.5: aggregate analyst reports -> top-N symbols. Tolerates a dict-wrapped
    payload ({"reports": [...]}) so a natural orchestrator wrapping doesn't crash cryptically."""
    from futures_fund.contracts import AnalystReport
    if isinstance(reports, dict):
        reports = reports.get("reports", [])
    if not isinstance(reports, list):
        raise TypeError(f"analyst reports must be a flat list, got {type(reports).__name__}")
    parsed = [AnalystReport.model_validate(r) for r in reports]
    return screen_reports(parsed, top_n)


def gate_execute_step(exchange, settings: Settings, state_dir, memory_dir,
                      now: datetime, cycle_no: int, proposals: list[dict],
                      management: list[dict] | None = None) -> dict:
    """Phases 7-10: normalize proposal symbols (accept unified OR raw), convert to TradeProposals
    (inject funding), run the A1 gate + A3b execution via execute_proposals, persist. An
    unrecognized symbol is COUNTED in report['dropped'], never silently vanished.

    `proposals` are NEW opens; `management` are the holdings-review decisions on carried
    positions ({symbol, action: hold|close, new_stop?}). CLOSE => closed at mark + journaled;
    HOLD with a (tighter, correct-side) new_stop => the stop is trailed in place. With an explicit
    holdings review present, reconciliation never auto-closes a holding for being absent."""
    from futures_fund.contracts import AgentProposal
    from futures_fund.state import is_halted
    account = load_account(state_dir, settings.account_size_usdt)
    positions = load_positions(state_dir)
    settings = working_universe(exchange, settings, positions)  # held symbols must be priceable
    ctx = fetch_context(exchange, settings)
    unified_to_raw = {u: r for r, u in ctx.raw_to_unified.items()}

    # Deterministic HALT enforcement AT the trade boundary: if the monitor (or anything) tripped
    # the halt — even mid-cycle, after preflight passed — open NO new positions. Holdings-review
    # CLOSES still run (a halt should DE-risk, not freeze us out of exiting).
    halted = is_halted(state_dir)
    if halted:
        proposals = []

    # --- Holdings review: trail stops on HOLDs, collect the explicit CLOSE set ---------------
    has_review = management is not None
    management = management or []
    by_raw = {}
    for m in management:
        s = m.get("symbol", "")
        by_raw[s if s in ctx.specs_by_raw else unified_to_raw.get(s, s)] = m
    force_close, trailed = set(), 0
    new_positions = []
    for p in positions:
        m = by_raw.get(p.symbol)
        if m and m.get("action") == "close":
            force_close.add(p.symbol)
            new_positions.append(p)
            continue
        if m and m.get("action") == "hold" and m.get("new_stop") is not None:
            ns = float(m["new_stop"])
            tighter = (p.direction == "long" and p.entry > ns > p.stop) or \
                      (p.direction == "short" and p.entry < ns < p.stop)
            if tighter:
                p = p.model_copy(update={"stop": ns})  # trail only; never loosen
                trailed += 1
        new_positions.append(p)
    positions = new_positions

    # Validate/convert each proposal INDEPENDENTLY: one malformed proposal (bad schema, inverted
    # stop) must never abort the gate phase and leave holdings unmanaged — drop it and continue.
    trade_props = []
    rationale_by_symbol: dict = {}
    dropped = 0
    for p in proposals:
        try:
            ap = AgentProposal.model_validate(p)
            raw = ap.symbol if ap.symbol in ctx.specs_by_raw else unified_to_raw.get(ap.symbol)
            if raw is None:
                dropped += 1
                continue
            funding = ctx.fundings[ctx.raw_to_unified[raw]].current_rate
            tp = to_trade_proposal(ap, funding).model_copy(update={"symbol": raw})
        except Exception:  # noqa: BLE001 — malformed/invalid proposal: drop, keep the rest
            dropped += 1
            continue
        trade_props.append(tp)
        rationale_by_symbol[raw] = ap.rationale
    report = execute_proposals(ctx, trade_props, contributing_agents=["research_manager", "trader"],
                               positions=positions, account=account, state_dir=state_dir,
                               memory_dir=memory_dir, now=now, cycle_no=cycle_no,
                               agent_key=_AGENT_KEY, rationale_by_symbol=rationale_by_symbol,
                               close_absent=not has_review, force_close=force_close)
    report["dropped"] = dropped
    report["trailed"] = trailed
    report["halted"] = halted  # closed_by_review is set by execute_proposals (actual, not intent)
    return report


def reflect_step(memory_dir) -> dict:
    """Reflection: hand the Reflector subagent the winners/losers to contrast."""
    return reflection_payload(memory_dir)


def lessons_step(memory_dir, now, regime: str | None, tags: list[str], k: int = 5) -> list[dict]:
    """Retrieve the top-K regime-relevant lessons (as JSON dicts) for injection into the
    debate/trader subagent prompts, so the team learns from past decisions (spec §6)."""
    from futures_fund.lessons import retrieve_lessons
    return [lz.model_dump(mode="json") for lz in retrieve_lessons(memory_dir, now, regime, tags, k)]

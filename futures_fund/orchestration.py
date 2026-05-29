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


def preflight_step(exchange, settings: Settings, state_dir, memory_dir,
                   now: datetime, cycle_no: int) -> dict:
    """Phase 0-2: load state, audit exits (BEFORE the halt check so a halt still closes
    stop/tp/liq hits), then build the per-symbol briefs + health/regime for the analysts."""
    ensure_memory_layout(memory_dir)
    account = load_account(state_dir, settings.account_size_usdt)
    positions = load_positions(state_dir)
    report = {"cycle": cycle_no, "halted": False, "opened": 0, "closed": 0,
              "carried": 0, "stuck_close": 0, "equity": account.balance, "actions": []}
    ctx = fetch_context(exchange, settings)
    positions = audit_and_reflect(ctx, positions, account, memory_dir, now, report,
                                  agent_key=_AGENT_KEY)
    save_account(state_dir, account)
    save_positions(state_dir, positions)
    if is_halted(state_dir):
        return {"cycle": cycle_no, "halted": True, "briefs": [], "equity": account.balance,
                "open_positions": [{"symbol": p.symbol, "direction": p.direction}
                                   for p in positions],
                "audit": {"closed": report["closed"], "carried": report["carried"]}}
    health = portfolio_health(account.balance, account.peak_equity, positions, ctx.prices,
                              recent_hit_rate=hit_rate(memory_dir, _AGENT_KEY))
    briefs = []
    for s in settings.symbols:
        b = build_symbol_brief(exchange, s, settings.timeframe)
        b["exchange_id"] = ctx.specs[s].symbol  # raw id (e.g. BTCUSDT) agents MUST use for output
        briefs.append(b)
    return {
        "cycle": cycle_no, "halted": False, "equity": health.equity,
        "drawdown_from_peak": health.drawdown_from_peak, "health_tier": health.tier,
        "briefs": briefs,
        "open_positions": [{"symbol": p.symbol, "direction": p.direction, "qty": p.qty,
                            "entry": p.entry} for p in positions],
        "audit": {"closed": report["closed"], "carried": report["carried"]},
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
                      now: datetime, cycle_no: int, proposals: list[dict]) -> dict:
    """Phases 7-10: normalize proposal symbols (accept unified OR raw), convert to TradeProposals
    (inject funding), run the A1 gate + A3b execution via execute_proposals, persist. An
    unrecognized symbol is COUNTED in report['dropped'], never silently vanished."""
    from futures_fund.contracts import AgentProposal
    account = load_account(state_dir, settings.account_size_usdt)
    positions = load_positions(state_dir)
    ctx = fetch_context(exchange, settings)
    unified_to_raw = {u: r for r, u in ctx.raw_to_unified.items()}
    aps = [AgentProposal.model_validate(p) for p in proposals]
    trade_props = []
    rationale_by_symbol: dict = {}
    dropped = 0
    for ap in aps:
        raw = ap.symbol if ap.symbol in ctx.specs_by_raw else unified_to_raw.get(ap.symbol)
        if raw is None:
            dropped += 1
            continue
        funding = ctx.fundings[ctx.raw_to_unified[raw]].current_rate
        tp = to_trade_proposal(ap, funding).model_copy(update={"symbol": raw})
        trade_props.append(tp)
        rationale_by_symbol[raw] = ap.rationale
    report = execute_proposals(ctx, trade_props, contributing_agents=["research_manager", "trader"],
                               positions=positions, account=account, state_dir=state_dir,
                               memory_dir=memory_dir, now=now, cycle_no=cycle_no,
                               agent_key=_AGENT_KEY, rationale_by_symbol=rationale_by_symbol)
    report["dropped"] = dropped
    return report


def reflect_step(memory_dir) -> dict:
    """Reflection: hand the Reflector subagent the winners/losers to contrast."""
    return reflection_payload(memory_dir)

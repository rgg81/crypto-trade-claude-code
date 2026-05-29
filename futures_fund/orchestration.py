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
    """Phase 0-2: load state, audit exits, build the per-symbol briefs + health/regime the
    Watcher and analysts need. Returns a JSON-serializable context dict."""
    ensure_memory_layout(memory_dir)
    account = load_account(state_dir, settings.account_size_usdt)
    positions = load_positions(state_dir)
    if is_halted(state_dir):
        return {"cycle": cycle_no, "halted": True, "briefs": [], "equity": account.balance,
                "open_positions": []}
    report = {"cycle": cycle_no, "halted": False, "opened": 0, "closed": 0,
              "carried": 0, "stuck_close": 0, "equity": account.balance, "actions": []}
    ctx = fetch_context(exchange, settings)
    positions = audit_and_reflect(ctx, positions, account, memory_dir, now, report,
                                  agent_key=_AGENT_KEY)
    save_account(state_dir, account)
    save_positions(state_dir, positions)
    health = portfolio_health(account.balance, account.peak_equity, positions, ctx.prices,
                              recent_hit_rate=hit_rate(memory_dir, _AGENT_KEY))
    briefs = [build_symbol_brief(exchange, s, settings.timeframe) for s in settings.symbols]
    return {
        "cycle": cycle_no, "halted": False, "equity": health.equity,
        "drawdown_from_peak": health.drawdown_from_peak, "health_tier": health.tier,
        "briefs": briefs,
        "open_positions": [{"symbol": p.symbol, "direction": p.direction, "qty": p.qty,
                            "entry": p.entry} for p in positions],
        "audit": {"closed": report["closed"], "carried": report["carried"]},
    }


def screen_step(reports: list[dict], top_n: int = 5) -> list[str]:
    """Phase 4.5: aggregate analyst reports (raw dicts) -> top-N symbols for debate."""
    from futures_fund.contracts import AnalystReport
    parsed = [AnalystReport.model_validate(r) for r in reports]
    return screen_reports(parsed, top_n)


def gate_execute_step(exchange, settings: Settings, state_dir, memory_dir,
                      now: datetime, cycle_no: int, proposals: list[dict]) -> dict:
    """Phases 7-10: convert agent proposals -> TradeProposals, run the A1 gate + A3b execution
    via execute_proposals (the deterministic Risk Manager + Portfolio Manager), persist."""
    from futures_fund.contracts import AgentProposal
    account = load_account(state_dir, settings.account_size_usdt)
    positions = load_positions(state_dir)
    ctx = fetch_context(exchange, settings)
    aps = [AgentProposal.model_validate(p) for p in proposals]
    trade_props = []
    rationale_by_symbol = {}
    for ap in aps:
        unified = ctx.raw_to_unified.get(ap.symbol)
        funding = ctx.fundings[unified].current_rate if unified else 0.0
        trade_props.append(to_trade_proposal(ap, funding))
        rationale_by_symbol[ap.symbol] = ap.rationale
    report = execute_proposals(ctx, trade_props, contributing_agents=["research_manager", "trader"],
                               positions=positions, account=account, state_dir=state_dir,
                               memory_dir=memory_dir, now=now, cycle_no=cycle_no,
                               agent_key=_AGENT_KEY)
    return report


def reflect_step(memory_dir) -> dict:
    """Reflection: hand the Reflector subagent the winners/losers to contrast."""
    return reflection_payload(memory_dir)

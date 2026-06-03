from __future__ import annotations

from datetime import datetime

from futures_fund.brief import build_symbol_brief, last_completed_frame
from futures_fund.config import Settings
from futures_fund.contracts import to_trade_proposal
from futures_fund.costs import count_funding_events
from futures_fund.cycle import (
    _SLIPPAGE_BPS,
    audit_and_reflect,
    execute_proposals,
    fetch_context,
)
from futures_fund.hitrate import hit_rate
from futures_fund.memory_layout import ensure_memory_layout
from futures_fund.portfolio import portfolio_health
from futures_fund.reduce import reduce_position
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
    # Anchor the mark to the COMPLETED 4h bar (last_close) the desk decides on — NOT the live
    # Binance mark_price (an index/funding price) — so r_progress matches how triggers/exits
    # evaluate on the 4h close and reconciles with the audited close. mark_price is a fallback.
    mark = float(brief.get("last_close") or brief.get("mark_price"))
    sign = 1.0 if pos.direction == "long" else -1.0
    # r_progress measures R earned vs the ORIGINAL risk. Anchor the denominator to the journaled
    # ORIGINAL stop (never trailed), not pos.stop — once a winner's stop trails past entry the
    # current-stop denominator collapses/flips sign (the +4.25 garbage). Take only the stop from
    # the journal (entry stays pos.entry, the filled price, to match the numerator's reference and
    # avoid proposal-vs-fill slippage). Fallback to the current stop for legacy/missing decisions.
    original_stop = None
    if decision:
        try:
            s = decision.get("stop")
            original_stop = float(s) if s is not None else None
        except (TypeError, ValueError):
            original_stop = None
    denom_stop = original_stop if original_stop is not None else pos.stop
    risk_per_unit = abs(pos.entry - denom_stop) or 1e-9
    tf = _TF_HOURS.get(timeframe, 4.0)
    bars_held = (now - pos.opened_ts).total_seconds() / 3600.0 / tf
    from futures_fund.portfolio import _is_risk_bearing
    card = {
        "direction": pos.direction, "qty": pos.qty, "entry": pos.entry, "stop": pos.stop,
        # at_risk: does this leg carry downside (loss-side stop)? Drives the risk-bearing tilt — a
        # trail that moves the stop to/through entry flips it False and neutralizes tilt_rb.
        "at_risk": _is_risk_bearing(pos),
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
    # Soft dollar-neutral exposure read (market-neutral mandate): gross long $ vs short $ + net tilt,
    # surfaced to the agents and nagged symmetrically via the scorecard. Visibility, not a veto.
    from futures_fund.portfolio import book_exposure, total_equity
    _prices = dict(ctx.prices)
    exposure = book_exposure(positions, _prices, total_equity(account.balance, positions, _prices))
    # Score any pending edge-aligned FLAT decisions against fresh marks — closes the learning loop
    # so the Reflector can mint enabling 'DO take it' lessons (a FLAT that moved our way cost us).
    try:
        from futures_fund.flat_journal import evaluate_pending_flats
        evaluate_pending_flats(memory_dir, dict(ctx.prices), now)
    except Exception:
        pass  # learning evaluation must never break the trading cycle
    from futures_fund.scorecard import build_scorecard
    if is_halted(state_dir):
        return {"cycle": cycle_no, "halted": True, "briefs": [], "equity": account.balance,
                "open_positions": [{"symbol": p.symbol, "direction": p.direction}
                                   for p in positions],
                "audit": {"closed": report["closed"], "carried": report["carried"]},
                "market_context": market_context, "exposure": exposure,
                "regime_state": _classify_regime_safe(state_dir, market_context, [], now, cycle_no),
                "scorecard": _with_exposure_warning(build_scorecard(state_dir, memory_dir), exposure)}
    health = portfolio_health(account.balance, account.peak_equity, positions, ctx.prices,
                              recent_hit_rate=hit_rate(memory_dir, _AGENT_KEY))
    scorecard = _with_exposure_warning(build_scorecard(state_dir, memory_dir, monthly_target=0.05),
                                       exposure)
    from futures_fund.journal import read_open_decisions
    held_by_raw = {p.symbol: p for p in positions}
    decisions_by_id = {d.get("id"): d for d in read_open_decisions(memory_dir)}
    briefs = []
    for s in settings.symbols:
        b = build_symbol_brief(exchange, s, settings.timeframe, now=now)  # last COMPLETED bar
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
        "exposure": exposure,
        "regime_state": _classify_regime_safe(state_dir, market_context, briefs, now, cycle_no),
        "scorecard": scorecard,
    }


def _news_tristate(market_context) -> bool | None:
    """Read a pre-computed news_risk_off from market_context as a TRI-STATE (True/False/None),
    preserving an explicit False (feed present, no catalyst) instead of collapsing it to None
    (genuinely missing). Today build_market_context emits no such key, so preflight stays None —
    identical to the prior behavior. The real news signal is folded in later by reclassify_step
    (Phase 4.6) from the News analyst's risk_off_flag."""
    if isinstance(market_context, dict) and "news_risk_off" in market_context:
        v = market_context["news_risk_off"]
        return None if v is None else bool(v)
    return None


def _classify_regime_safe(state_dir, market_context, briefs, now, cycle_no) -> dict:
    """Deterministic regime classification for the agents to read (the agent-override layer is an
    orchestrator step). Never breaks the cycle — returns a neutral state on any failure."""
    try:
        from futures_fund.regime import classify_regime
        return classify_regime(state_dir, market_context, briefs, now, cycle_no=cycle_no,
                               news_risk_off=_news_tristate(market_context)).model_dump(mode="json")
    except Exception:  # noqa: BLE001
        return {"regime": "mixed", "confirmed": False, "score": 0.0,
                "drivers": {"error": "classify_failed"}, "candle": "", "cycle_no": cycle_no}


def reclassify_step(state_dir, context: dict, analyst_reports, now: datetime | None = None) -> dict:
    """Phase 4.6 — re-classify the regime AFTER the analyst pass, folding the News analyst's
    risk_off_flag into the deterministic news term (which preflight could not see — it runs before
    the analysts). Returns the updated regime_state dict; the caller overwrites context.json so the
    debate, Trader, and gate all read the news-informed regime.

    Idempotent and fail-safe: re-uses the preflight SERVED CANDLE so the persistence chain stays
    candle-consistent (and a RETRY re-running the cycle reproduces the same record, since
    append_regime_history replaces by cycle_no). On ANY failure it returns the PRIOR regime_state
    unchanged — never downgrades a risk_off pass-1 to mixed, never raises into the cycle. The news
    term is asymmetric (-1/0), so a re-classification can only deepen risk-off, never undo it."""
    prior = (context or {}).get("regime_state") or {}
    if not isinstance(context, dict):
        return prior  # no usable context to re-classify from -> keep whatever preflight produced
    try:
        from datetime import UTC
        from futures_fund.regime import classify_regime
        from futures_fund.regime_news import (
            aggregate_news_risk_off,
            apply_news_stickiness,
            load_last_shock_cycle,
            save_last_shock_cycle,
        )
        market_context = (context or {}).get("market_context") or {}
        briefs = (context or {}).get("briefs") or []
        # cycle_no feeds the int-typed RegimeState; an absent/null context['cycle'] must not make
        # classify_regime raise (which would silently drop the news fold). Fall back to the prior
        # state's cycle_no, then 0.
        cycle_no = (context or {}).get("cycle")
        if cycle_no is None:
            cycle_no = prior.get("cycle_no") or 0
        cycle_no = int(cycle_no)
        warnings = market_context.get("warnings") if isinstance(market_context, dict) else []
        news_off = aggregate_news_risk_off(analyst_reports, briefs, warnings)
        # Sticky/decaying news shock: an unresolved market-wide shock must NOT silently lapse when
        # its headline scrolls out of the rolling feed (a degraded None read). Keep it elevated
        # through degraded reads within the decay window; an explicit False (analyst judged no
        # shock) resolves it. Writes are idempotent per cycle (a RETRY reproduces the same state).
        last_shock = load_last_shock_cycle(state_dir)
        news_off, new_last_shock = apply_news_stickiness(news_off, cycle_no, last_shock)
        if new_last_shock != last_shock:
            save_last_shock_cycle(state_dir, new_last_shock)
        # Re-use the preflight served candle so this cycle's risk_off vote lands on the SAME 4h
        # candle the deterministic chain expects; fall back to a fresh clock only if it is absent.
        when = None
        candle_str = prior.get("candle")
        if candle_str:
            try:
                when = datetime.fromisoformat(candle_str)
            except ValueError:
                when = None
        if when is None:
            when = now or datetime.now(UTC)
        rs = classify_regime(state_dir, market_context, briefs, when,
                             cycle_no=cycle_no, news_risk_off=news_off)
        rs_dict = rs.model_dump(mode="json")
        # Mark that Phase 4.6 (the news-fold) RAN, so the fail-loud guard can distinguish a
        # legitimate degraded-feed None from a SKIPPED reclassify (where this marker is absent).
        rs_dict.setdefault("drivers", {})["reclassified"] = True
        return rs_dict
    except Exception:  # noqa: BLE001 — never break the cycle; keep the pass-1 regime
        return prior


def reclassify_skipped(regime_state, analyst_reports) -> bool:
    """Fail-loud guard for a SKIPPED reclassify (Phase 4.6). True when the analysts produced a news
    judgement (>=1 news-agent report) but the news-fold was never applied — regime_state carries no
    `reclassified` marker AND news_risk_off is still None. Distinguishes a genuinely skipped
    reclassify from (a) a legitimate degraded-feed fold (marker present), (b) a clean fold
    (news_risk_off in {True, False}), and (c) a HALT/stand-down with no analyst pass (no news
    reports -> never blocks). Used by gate_execute_cli to BLOCK the gate so a skipped news-fold
    cannot pass silently with a stale (news-blind) regime."""
    if not isinstance(regime_state, dict):
        return False
    drivers = regime_state.get("drivers") or {}
    if drivers.get("reclassified") is True:
        return False  # Phase 4.6 explicitly ran (even if it folded to a degraded None)
    if drivers.get("news_risk_off") is not None:
        return False  # news judged True/False -> folded (backward-compat for pre-marker contexts)
    reports = analyst_reports if isinstance(analyst_reports, list) else []
    return any(isinstance(r, dict) and r.get("agent") == "news" for r in reports)


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


def management_review(payload: dict) -> list[dict]:
    """Extract the holdings-review list from a Trader `proposals.json` payload for the AGENT path.

    The agent path ALWAYS carries a holdings review (possibly empty). A missing or null
    `management` key must NEVER become `management=None` at the gate, because that flips
    `has_review` off and reconciliation then closes EVERY held position by absence — flattening
    the whole book on a stand-down/HALT (the opposite of intent). Coerce a missing/null key to an
    empty review so held positions are kept; only `execute_proposals`' own default (the baseline
    path) may close by absence, never the agent CLI."""
    m = payload.get("management")
    return [] if m is None else m


def _fold_raw_symbols(exchange, settings: Settings, raw_symbols) -> Settings:
    """Fold extra RAW symbols (e.g. pending-trigger symbols) into the universe so their 4h bars
    are fetched and the trigger can be evaluated — same mechanism working_universe uses for held."""
    syms = list(settings.symbols)
    seen = set(syms)
    unify = getattr(exchange, "unified_for_raw", None)
    for raw in raw_symbols:
        u = unify(raw) if unify else None
        if u and u not in seen:
            syms.append(u)
            seen.add(u)
    return settings.model_copy(update={"symbols": syms}) if syms != list(settings.symbols) else settings


def _with_exposure_warning(scorecard: dict, exposure: dict) -> dict:
    """Fold the SYMMETRIC dollar-neutral nag into the scorecard warnings (soft steer; no veto). A
    materially net-long book is nagged exactly as hard as a net-short one; balanced/flat is silent."""
    from futures_fund.portfolio import exposure_warning
    w = exposure_warning(exposure or {})
    if w and isinstance(scorecard, dict):
        scorecard.setdefault("warnings", []).append(w)
    return scorecard


def _counter_regime(direction: str, regime) -> bool:
    """A trade is COUNTER-regime when it fights the desk's directional read: a LONG while the regime
    is risk_off, OR a SHORT while risk_on. In 'mixed' (no directional read) NEITHER is counter, so
    both go at market. Perfectly symmetric: risk_off long-confirm mirrors risk_on short-confirm."""
    return (regime == "risk_off" and direction == "long") or \
           (regime == "risk_on" and direction == "short")


def _proposal_to_stop_entry(p: dict, cycle_no: int):
    """Convert a fresh MARKET proposal into a stop_entry trigger at its own level — the tape must
    confirm the break (one 4h CLOSE through entry) before the desk commits AGAINST its regime read.
    stop_entry semantics already fire a LONG on a close ABOVE the level and a SHORT on a close BELOW,
    so a counter-regime long confirms on an up-break and a counter-regime short on a down-break."""
    from futures_fund.pending_orders import PendingOrder
    return PendingOrder(
        symbol=p.get("symbol", ""), direction=p.get("direction", ""), kind="stop_entry",
        trigger_level=float(p["entry"]), stop=float(p["stop"]),
        take_profits=[float(x) for x in (p.get("take_profits") or [])],
        atr=float(p.get("atr", 0.0) or 0.0),
        falsifiable_prediction=p.get("falsifiable_prediction") or "",
        rationale="[counter-regime -> confirm on 4h close through entry] " + (p.get("rationale") or ""),
        confidence=float(p.get("confidence", 0.5) or 0.5),
        created_cycle=cycle_no, expires_cycle=cycle_no + 2)


def _apply_counter_regime_confirmation(proposals: list[dict], regime_state, cycle_no: int):
    """SYMMETRIC entry-style gate (replaces the one-sided shorts drop-filter). Permission is never
    blocked; a COUNTER-regime fresh market proposal is rewritten into a confirmation stop_entry
    trigger instead of opening at market.

    regime_state None (regime feature NOT wired — legacy/cold-start caller) -> pass-through at
    market, preserving the original contract (production ALWAYS passes a dict). A PROVIDED dict that
    is untrustworthy (no quorum / errored / unknown label — e.g. the classify-failed fallback) ->
    FAIL-CLOSED symmetric: BOTH directions require confirmation, so a degraded regime read can never
    open a naked market position either way. Returns (market_proposals, armed_triggers)."""
    if not isinstance(regime_state, dict):
        return list(proposals), []   # regime not wired -> preserve prior pass-through behavior
    regime = regime_state.get("regime")
    drivers = regime_state.get("drivers")
    quorum_ok = bool(drivers.get("quorum_met")) if isinstance(drivers, dict) else False
    trustworthy = regime in ("risk_off", "risk_on", "mixed") and quorum_ok
    market, armed = [], []
    for p in proposals:
        counter = _counter_regime(p.get("direction"), regime) if trustworthy else True
        if counter:
            try:
                armed.append(_proposal_to_stop_entry(p, cycle_no))
            except (KeyError, TypeError, ValueError):
                pass  # a malformed proposal can't be armed as a trigger -> drop it (gate would too)
        else:
            market.append(p)
    return market, armed


def _valid_reduce_fraction(v) -> float | None:
    """Coerce a reduce_fraction directive value to a float in (0, 1); None if invalid."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if 0.0 < f < 1.0 else None


def _is_tighter_stop(direction: str, cur_stop: float, new_stop: float, mark: float | None) -> bool:
    """A trailed stop is valid only if it is TIGHTER than the current stop and short of the mark —
    a winning long locks profit ABOVE entry, a winning short BELOW; a stop past the mark would
    insta-stop. Shared by the HOLD trail and the reduce-v2 bank-and-trail."""
    if mark is None:
        return False
    return ((direction == "long" and cur_stop < new_stop < mark) or
            (direction == "short" and mark < new_stop < cur_stop))


def gate_execute_step(exchange, settings: Settings, state_dir, memory_dir,
                      now: datetime, cycle_no: int, proposals: list[dict],
                      management: list[dict] | None = None,
                      regime_state: dict | None = None,
                      triggers: list[dict] | None = None,
                      cancel_triggers: list[dict] | None = None) -> dict:
    """Phases 7-10: normalize proposal symbols (accept unified OR raw), convert to TradeProposals
    (inject funding), run the A1 gate + A3b execution via execute_proposals, persist. An
    unrecognized symbol is COUNTED in report['dropped'], never silently vanished.

    `proposals` are NEW opens; `management` are the holdings-review decisions on carried
    positions ({symbol, action: hold|close, new_stop?}). CLOSE => closed at mark + journaled;
    HOLD with a (tighter, correct-side) new_stop => the stop is trailed in place. With an explicit
    holdings review present, reconciliation never auto-closes a holding for being absent."""
    from futures_fund.contracts import AgentProposal
    from futures_fund.pending_orders import (
        check_pending_orders,
        fired_to_proposal,
        load_pending_orders,
        save_pending_orders,
        upsert_triggers,
    )
    from futures_fund.state import is_halted
    account = load_account(state_dir, settings.account_size_usdt)
    positions = load_positions(state_dir)
    pending = load_pending_orders(state_dir)
    settings = working_universe(exchange, settings, positions)  # held symbols must be priceable
    settings = _fold_raw_symbols(exchange, settings, [o.symbol for o in pending])  # +pending syms
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
    reduced, banked_pnl, reduce_dropped = 0, 0.0, 0
    reduce_actions, reduce_warnings = [], []
    new_positions = []
    for p in positions:
        m = by_raw.get(p.symbol)
        if m and m.get("action") == "close":
            force_close.add(p.symbol)
            new_positions.append(p)
            continue
        if m and m.get("action") == "reduce":
            frac = _valid_reduce_fraction(m.get("reduce_fraction"))
            mark = ctx.prices.get(p.symbol)
            fr = ctx.fundings.get(ctx.raw_to_unified.get(p.symbol))
            spec = ctx.specs_by_raw.get(p.symbol)
            if frac is None or mark is None or fr is None or spec is None:
                reduce_dropped += 1
                new_positions.append(p)  # malformed/unpriceable reduce: leave the position whole
                continue
            n_events = count_funding_events(p.opened_ts, now, int(fr.interval_hours))
            res = reduce_position(p, mark, frac, funding_rate=fr.current_rate,
                                  funding_events=n_events, slippage_bps=_SLIPPAGE_BPS, spec=spec)
            if res.kind == "promote_full":
                # The runner would be sub-min-notional dust -> close 100% via the normal force_close
                # path. This emits a "reduce" intent action here AND execute_proposals emits the
                # actual "close" action (two entries for one symbol — intentional). The wallet is
                # credited exactly once, by execute_proposals' close, NOT here. (No survivor to
                # trail.)
                force_close.add(p.symbol)
                new_positions.append(p)
                reduce_actions.append({"reduce": p.symbol, "fraction": frac, "full": True})
                continue
            if res.kind == "noop_dust":
                reduce_warnings.append(f"reduce noop (dust) {p.symbol}")
                survivor = p  # nothing banked; the un-reduced position survives
            else:  # "reduced": bank the slice, carry the runner
                account.balance += res.closed_trade.realized_pnl
                reduced += 1
                banked_pnl += res.closed_trade.realized_pnl
                reduce_actions.append({"reduce": p.symbol, "fraction": frac,
                                       "pnl": res.closed_trade.realized_pnl, "full": False})
                survivor = res.runner
            # reduce v2: an OPTIONAL new_stop trails the SURVIVOR's stop in the SAME directive
            # (bank-and-trail), reusing the tighten-only/short-of-mark guard.
            ns = m.get("new_stop")
            if ns is not None and _is_tighter_stop(survivor.direction, survivor.stop,
                                                   float(ns), mark):
                survivor = survivor.model_copy(update={"stop": float(ns)})
                trailed += 1
            new_positions.append(survivor)
            continue
        if m and m.get("action") == "hold" and m.get("new_stop") is not None:
            ns = float(m["new_stop"])
            mark = ctx.prices.get(p.symbol)
            if _is_tighter_stop(p.direction, p.stop, ns, mark):
                p = p.model_copy(update={"stop": ns})  # trail only; never loosen
                trailed += 1
        new_positions.append(p)
    positions = new_positions

    # --- Trigger orders: fire armed conditionals off the latest 4h bar, then they become NORMAL
    # proposals (at the trigger price) competing in the SAME gate. Held-symbol orders are skipped
    # (no stacking). On HALT a fired trigger is consumed but NOT opened (like a fresh open). ----
    held_symbols = {p.symbol for p in positions}
    bars_by_symbol: dict = {}
    for raw, uni in ctx.raw_to_unified.items():
        # A stop_entry/limit_entry must fire off the latest COMPLETED 4h bar — NOT the still-forming
        # candle the OHLCV feed returns as iloc[-1] (its transient close flips on every tick). Drop
        # the forming candle so triggers evaluate a real CLOSE. (ctx.prices keeps iloc[-1] for exits.)
        df = last_completed_frame(ctx.frames.get(uni), now, settings.timeframe)
        if df is None or not len(df):
            continue
        try:
            bar = df.iloc[-1]
            bars_by_symbol[raw] = {"high": float(bar["high"]), "low": float(bar["low"]),
                                   "close": float(bar["close"])}
        except (KeyError, TypeError, ValueError):
            pass
    fired, expired, remaining = check_pending_orders(state_dir, bars_by_symbol, cycle_no,
                                                     held_symbols=held_symbols)

    # --- Explicit cancellation: the Trader/RM may RETIRE an armed trigger whose thesis decayed, via
    # the normal proposals flow (`cancel_triggers`), so the TEAM cancels — not a manual store edit.
    # AUTHORITATIVE for the whole cycle: a canceled (symbol, direction?, kind?) does NOT fire this
    # cycle, does NOT persist, and is NOT re-armed (even by a same-cycle counter-regime conversion or
    # restated Trader trigger — filtered again before the save below). Match on symbol + optional
    # direction/kind. Explicit retirement wins over a confirmed break or an auto cr-safety arm.
    def _is_canceled(o):
        for c in (cancel_triggers or []):
            if isinstance(c, dict) and c.get("symbol") == o.symbol \
               and c.get("direction") in (None, o.direction) \
               and c.get("kind") in (None, o.kind):
                return True
        return False
    n_canceled = 0
    if cancel_triggers:
        kept_fired = [o for o in fired if not _is_canceled(o)]      # a canceled fire does NOT open
        kept_rem = [o for o in remaining if not _is_canceled(o)]    # nor persist
        n_canceled = (len(fired) - len(kept_fired)) + (len(remaining) - len(kept_rem))
        fired, remaining = kept_fired, kept_rem

    # --- SYMMETRIC counter-regime entry-style gate (replaces the one-sided shorts drop-filter):
    # a market entry AGAINST the regime read (short while not risk_off, long while risk_off) is
    # converted to a confirmation stop_entry trigger, never opened at market — identically for both
    # directions. Permission is never blocked; only entry STYLE (market vs confirm) is conditioned.
    # EXEMPTION by kind: a fired STOP_ENTRY is itself a confirmed CLOSE-through-level break, so it is
    # exempt and goes to market. A fired LIMIT_ENTRY is a TOUCH (pullback) fill — NOT a confirmed
    # break — so a counter-regime limit fill is re-routed through the transform like a fresh proposal
    # (a with-regime limit fill still fills at market). This closes the unconfirmed-knife-catch path.
    stop_fired = [] if halted else [fired_to_proposal(o) for o in fired if o.kind == "stop_entry"]
    touch_fired = [] if halted else [fired_to_proposal(o) for o in fired if o.kind != "stop_entry"]
    to_confirm = ([] if halted else list(proposals)) + touch_fired
    market_fresh, cr_armed = _apply_counter_regime_confirmation(to_confirm, regime_state, cycle_no)
    proposals = market_fresh + stop_fired
    fired_props = stop_fired + touch_fired  # all fires, for telemetry (triggers_fired counts both)

    # Validate/convert each proposal INDEPENDENTLY: one malformed proposal (bad schema, inverted
    # stop) must never abort the gate phase and leave holdings unmanaged — drop it and continue.
    trade_props = []
    rationale_by_symbol: dict = {}
    prediction_by_symbol: dict = {}
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
        prediction_by_symbol[raw] = ap.falsifiable_prediction
    report = execute_proposals(ctx, trade_props, contributing_agents=["research_manager", "trader"],
                               positions=positions, account=account, state_dir=state_dir,
                               memory_dir=memory_dir, now=now, cycle_no=cycle_no,
                               agent_key=_AGENT_KEY, rationale_by_symbol=rationale_by_symbol,
                               prediction_by_symbol=prediction_by_symbol,
                               close_absent=not has_review, force_close=force_close)
    report["dropped"] = dropped
    report["trailed"] = trailed
    report["halted"] = halted  # closed_by_review is set by execute_proposals (actual, not intent)
    report["reduced"] = reduced
    report["banked_pnl"] = banked_pnl
    report["reduce_dropped"] = reduce_dropped
    if reduce_actions:
        report.setdefault("actions", []).extend(reduce_actions)
    if reduce_warnings:
        report.setdefault("warnings", []).extend(reduce_warnings)

    # --- Persist the trigger store (remaining + this cycle's NEW triggers; a halt arms none) and
    # the regime history (idempotent by cycle_no). fired/expired/held are already removed. ----
    # counter-regime conversions arm alongside Trader-emitted triggers (a halt produces neither:
    # cr_armed is [] when proposals were []'d on halt, and the Trader-trigger loop is halt-guarded).
    # PROTECT the cr_armed keys: upsert_triggers replaces by (symbol, direction, kind), so a Trader
    # trigger sharing a key would silently clobber the auto-armed counter-regime SAFETY trigger.
    # The safety conversion wins; a colliding Trader trigger is skipped (counted as armed_collisions).
    new_triggers = list(cr_armed)
    cr_keys = {(o.symbol, o.direction, o.kind) for o in cr_armed}
    armed_collisions = 0
    if not halted:
        from futures_fund.pending_orders import PendingOrder
        for t in (triggers or []):
            try:
                fields = {**t, "created_cycle": cycle_no,
                          "expires_cycle": int(t.get("expires_cycle", cycle_no + 3))}
                po = PendingOrder.model_validate(fields)
                if (po.symbol, po.direction, po.kind) in cr_keys:
                    armed_collisions += 1
                    continue  # don't clobber the counter-regime safety trigger
                new_triggers.append(po)
            except Exception:  # noqa: BLE001 — drop a malformed trigger, keep the rest
                pass
    # cancel is AUTHORITATIVE: a canceled key must not be re-armed this cycle, even by a cr-safety
    # conversion or a restated Trader trigger. Strip them so triggers_armed reflects the real store.
    if cancel_triggers:
        _kept_new = [o for o in new_triggers if not _is_canceled(o)]
        n_canceled += len(new_triggers) - len(_kept_new)
        new_triggers = _kept_new
    save_pending_orders(state_dir, upsert_triggers(remaining, new_triggers))
    if isinstance(regime_state, dict):
        try:
            from futures_fund.regime import RegimeState, append_regime_history
            append_regime_history(state_dir, RegimeState.model_validate(regime_state))
        except Exception:  # noqa: BLE001 — history is advisory; never break the gate
            pass
    report["triggers_fired"] = len(fired_props)
    report["triggers_expired"] = len(expired)
    report["triggers_remaining"] = len(remaining)
    report["triggers_armed"] = len(new_triggers)
    report["triggers_canceled"] = n_canceled
    # symmetric telemetry replacing dropped_short_regime: how many fresh entries went to market vs
    # were converted to a counter-regime confirmation trigger (operator can see the routing split).
    report["market_entries"] = len(market_fresh)
    report["counter_regime_triggered"] = len(cr_armed)
    report["armed_collisions"] = armed_collisions  # Trader triggers skipped to protect a cr-safety key
    # Surface whether the Phase 4.6 news fold actually engaged, so a silently-skipped reclassify is
    # distinguishable from a correctly-folded cycle. news_risk_off in {True, False} == news was
    # judged; None == degraded (correct/expected on a HALT/stand-down with no analyst pass, but a
    # red flag on a normal cycle — it means reclassify never ran).
    if isinstance(regime_state, dict):
        drv = regime_state.get("drivers") or {}
        report["news_risk_off"] = drv.get("news_risk_off")
        report["regime_degraded"] = drv.get("degraded") or []
        report["news_folded"] = drv.get("news_risk_off") is not None
    # POST-trade dollar-neutral exposure of the resulting book (market-neutral mandate): the operator
    # and next cycle can see how net-long/short the desk is sitting after this cycle's opens/closes.
    try:
        from futures_fund.portfolio import book_exposure, total_equity
        final_positions = load_positions(state_dir)
        final_account = load_account(state_dir, settings.account_size_usdt)
        eq = total_equity(final_account.balance, final_positions, dict(ctx.prices))
        report["exposure"] = book_exposure(final_positions, dict(ctx.prices), eq)
    except Exception:  # noqa: BLE001 — telemetry must never break the gate
        pass
    return report


def reflect_step(memory_dir) -> dict:
    """Reflection: hand the Reflector subagent the winners/losers to contrast."""
    return reflection_payload(memory_dir)


def lessons_step(memory_dir, now, regime: str | None, tags: list[str], k: int = 5) -> list[dict]:
    """Retrieve the top-K regime-relevant lessons (as JSON dicts) for injection into the
    debate/trader subagent prompts, so the team learns from past decisions (spec §6)."""
    from futures_fund.lessons import retrieve_lessons
    return [lz.model_dump(mode="json") for lz in retrieve_lessons(memory_dir, now, regime, tags, k)]

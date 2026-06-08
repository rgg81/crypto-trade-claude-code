from __future__ import annotations

from datetime import datetime

from futures_fund.baseline import swing_levels
from futures_fund.brief import build_symbol_brief, last_completed_frame, oi_change_for
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


def _regime_panel_briefs(exchange, briefs: list[dict], timeframe: str, now: datetime) -> list[dict]:
    """Briefs for the canonical regime MAJORS absent from this cycle's universe, so the
    deterministic regime (breadth + quorum) is read over the STABLE full panel — NOT just whichever
    majors happen to be in the Watcher's shortlist. A thin shortlist (e.g. only BTC+BNB) otherwise
    loses quorum (>=3 majors + BTC) and collapses the label to 'mixed' on a deeply risk_off tape
    (the cycle-29 artifact); persisting these into context['briefs'] also fixes the reclassify
    recompute, which re-derives quorum from the same briefs. Each is tagged `regime_panel_only` —
    priced for the regime read, NEVER traded (no proposal sourced from the universe). FAIL-SAFE: a
    major the exchange can't map (no unified_for_raw) or can't price is skipped, so the regime
    degrades for it exactly as today and a missing/delisted major never breaks preflight."""
    from futures_fund.regime import _MAJORS
    unify = getattr(exchange, "unified_for_raw", None)
    if unify is None:
        return []
    covered = {b.get("exchange_id") for b in briefs}
    extra = []
    for raw in _MAJORS:
        if raw in covered:
            continue
        uni = unify(raw)
        if not uni:
            continue
        try:
            b = build_symbol_brief(exchange, uni, timeframe, now=now)  # last COMPLETED bar
        except Exception:  # noqa: BLE001 — an unpriceable/missing major must never break preflight
            continue
        b["exchange_id"] = raw          # raw id (e.g. ETHUSDT) — the key the regime reads
        b["regime_panel_only"] = True   # priced for the regime read only; never traded
        extra.append(b)
    return extra


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
        # finalize a declined-flat's verdict only after a multi-day horizon (not the 1-cycle bounce)
        evaluate_pending_flats(memory_dir, dict(ctx.prices), now, now_cycle=cycle_no)
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
    # Pillar 1 DEPLOY: month-to-date risk pacing — surfaces a deploy directive (soft/normal/press/
    # throttle) the team reads to actively pursue 5%/mo. Advisory/utilization-only; anti-martingale
    # (drawdown never presses); the gate's protected caps are unchanged. Fail-safe -> soft on error.
    try:
        from futures_fund.pacing import pacing_state
        _ps = pacing_state(state_dir, now, health, monthly_target=0.05)
        pacing = {"mode": _ps.mode, "appetite": _ps.appetite,
                  "suggested_risk_mult": _ps.suggested_risk_mult, "mtd_return": _ps.mtd_return,
                  "pace": _ps.pace, "pace_gap": _ps.pace_gap, "in_drawdown": _ps.in_drawdown,
                  "directive": _ps.directive}
    except Exception:  # noqa: BLE001 — pacing is advisory; never break the cycle
        pacing = {"mode": "soft", "directive": "SOFT — pacing unavailable; trade conservatively.",
                  "suggested_risk_mult": 0.5}
    # Pillar 3 IMPROVE: read-only improvement panel (deployment rate, corpus two-sidedness, return
    # trend) so the team + the month-end meta-reflection can see whether the desk is getting better.
    try:
        from futures_fund.improvement import improvement_panel
        improvement = improvement_panel(state_dir, memory_dir)
    except Exception:  # noqa: BLE001 — advisory; never break the cycle
        improvement = {}
    from futures_fund.journal import read_open_decisions
    held_by_raw = {p.symbol: p for p in positions}
    decisions_by_id = {d.get("id"): d for d in read_open_decisions(memory_dir)}
    briefs = []
    for s in settings.symbols:
        b = build_symbol_brief(exchange, s, settings.timeframe, now=now)  # last COMPLETED bar
        b["exchange_id"] = ctx.specs[s].symbol  # raw id (e.g. BTCUSDT) agents MUST use for output
        # Pillar 2 ADAPT: attach the regime-routed in-season playbook for this symbol's quadrant, so
        # the team switches strategy with the tape (trend->trend-follow, range->mean-reversion).
        try:
            from futures_fund.playbook import playbook_for
            b["playbook"] = playbook_for(b.get("regime", ""))
        except Exception:  # noqa: BLE001 — advisory; never break the brief
            pass
        pos = held_by_raw.get(b["exchange_id"])
        if pos is not None:  # carried position -> attach the HOLD/CLOSE review card
            b["holding"] = _holding_card(pos, b, now, settings.timeframe,
                                         decisions_by_id.get(pos.decision_id))
        briefs.append(b)
    # Guarantee the regime is read over the STABLE canonical majors panel (not just the shortlist):
    # append briefs for any canonical major absent from the universe so quorum/breadth see them. It
    # feeds BOTH the preflight regime call below and (via context['briefs']) the Phase-4.6
    # reclassify recompute. Fail-safe: unpriceable majors are skipped (regime degrades as before).
    briefs.extend(_regime_panel_briefs(exchange, briefs, settings.timeframe, now))
    # DATA-INTEGRITY: null any positioning the globalLongShortAccountRatio feed ALIASED across
    # distinct symbols (cy50: DOGE returned ETH's L/S+long_account verbatim) so the team can't
    # trade on a feed bug — done before archiving so the archive records the cleaned values too.
    from futures_fund.brief import flag_duplicate_positioning
    flag_duplicate_positioning(briefs)
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
        "pacing": pacing,
        "improvement": improvement,
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


def funnel_skipped(analyst_reports, proposals, triggers) -> bool:
    """Fail-loud guard for a WHOLE skipped analyst funnel (Phases 4-4.6). True when the cycle sends
    TRADES (non-empty proposals OR triggers) but analyst_reports.json is absent/empty — i.e. the
    analyst pass / screen / reclassify were skipped entirely yet orders are being sent. A genuine
    stand-down (and a HALT) submits EMPTY proposals AND triggers, so it never blocks. Complements
    reclassify_skipped (which catches a skipped news-FOLD only when the reports DO exist) — closing
    the gap that let an entirely-skipped funnel pass silently on a news-blind preflight regime."""
    has_trades = bool(proposals) or bool(triggers)
    return has_trades and not analyst_reports


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
        # preserve any per-trade risk REDUCTION across the counter-regime->trigger rewrite, so a
        # half-size starter doesn't silently fire at full size when confirmed (gate still clamps)
        risk_mult=float(p.get("risk_mult", 1.0) or 1.0),
        # carry an explicit OI-confirmation opt-in if the Trader set one; absent -> False so a
        # counter-regime SAFETY trigger is never double-gated on OI (no spurious feed-outage block)
        require_oi_rising=bool(p.get("require_oi_rising", False)),
        created_cycle=cycle_no, expires_cycle=cycle_no + 2)


def _stamp_anchor_swing(po, swings_by_symbol: dict):
    """Stamp a breakout/breakdown stop_entry's ARM-TIME directional swing (swing_low for a short,
    swing_high for a long) so a later cycle can auto-cancel it once the swing crosses past it.
    Applied identically to BOTH provenances — Trader-emitted triggers AND counter-regime safety
    conversions — so neither is left silently un-revalidatable (no provenance gap, no long/short
    bias). No-op for a non-stop_entry, already-stamped order, or absent swing -> left unstamped
    (None) -> never auto-revalidated (fail-safe)."""
    if po.kind != "stop_entry" or po.anchor_swing is not None:
        return po
    sw = swings_by_symbol.get(po.symbol)
    if sw is None:
        return po
    return po.model_copy(update={"anchor_swing": sw[1] if po.direction == "short" else sw[0]})


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


_NOISE_BAND_ATR = 0.6  # a stop trailed closer than this many ATR to the mark risks a noise wick-out


def _position_atr(ctx, raw_symbol: str, now: datetime, timeframe: str) -> float | None:
    """ATR of the held symbol's last COMPLETED bar — for the advisory noise-band trail guard.
    Mirrors the trigger path's last_completed_frame use. Returns None when the frame is unavailable,
    which disables the guard (it never blocks a trail)."""
    try:
        from futures_fund.baseline import _atr
        uni = ctx.raw_to_unified.get(raw_symbol)
        df = last_completed_frame(ctx.frames.get(uni), now, timeframe)
        if df is None or not len(df):
            return None
        return float(_atr(df))
    except Exception:  # noqa: BLE001 — an ATR we can't compute just disables the advisory guard
        return None


def _noise_band_warning(symbol: str, new_stop: float, mark: float | None, atr) -> str | None:
    """ADVISORY (never a block): a stop trailed to within _NOISE_BAND_ATR of the mark on a high-ATR
    name risks a noise wick-out — the cycle-28 lesson (a 0.53-ATR stop on a ~7%-ATR name was the
    flagged noise-stop error). Returns a warning string when the trailed stop is STRICTLY inside the
    band (distance < _NOISE_BAND_ATR ATR from mark); a stop at/beyond the band is treated as safe.
    The exact-boundary case is immaterial — this is advisory and new_stop is a discretionary price,
    never computed as mark - band*atr. No-ops when ATR is unavailable/non-positive."""
    if mark is None or not atr or atr <= 0:
        return None
    dist = abs(mark - new_stop)
    if dist < _NOISE_BAND_ATR * atr:
        return (f"trail into noise band {symbol}: new_stop {new_stop:g} is {dist / atr:.2f} ATR "
                f"from mark {mark:g} (<{_NOISE_BAND_ATR} ATR) — wick-out risk")
    return None


def gate_execute_step(exchange, settings: Settings, state_dir, memory_dir,
                      now: datetime, cycle_no: int, proposals: list[dict],
                      management: list[dict] | None = None,
                      regime_state: dict | None = None,
                      triggers: list[dict] | None = None,
                      cancel_triggers: list[dict] | None = None,
                      ground_truth: dict | None = None) -> dict:
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
        non_crypto_triggers,
        revalidate_triggers,
        save_pending_orders,
        upsert_triggers,
    )
    from futures_fund.state import is_halted

    # Pillar 4 AUDIT — anti-hallucination: drop any proposal/trigger whose entry/atr diverges too
    # far from the brief ground truth it was derived from (fabricated entry = a fantasy paper fill;
    # a fabricated atr = mis-sized risk), BEFORE the gate. Fail-open on missing ground truth so it
    # never becomes a deploy-blocker; adds a check, weakens nothing. Symmetric long/short.
    audit_dropped: list[dict] = []
    if ground_truth:
        from futures_fund.proposal_audit import audit_batch
        proposals, _dp = audit_batch(list(proposals or []), ground_truth, is_trigger=False)
        triggers, _dt = audit_batch(list(triggers or []), ground_truth, is_trigger=True)
        audit_dropped = _dp + _dt

    account = load_account(state_dir, settings.account_size_usdt)
    positions = load_positions(state_dir)
    pending = load_pending_orders(state_dir)
    settings = working_universe(exchange, settings, positions)  # held symbols must be priceable
    settings = _fold_raw_symbols(exchange, settings, [o.symbol for o in pending])  # +pending syms
    ctx = fetch_context(exchange, settings)
    unified_to_raw = {u: r for r, u in ctx.raw_to_unified.items()}

    # CRYPTO-ONLY enforcement (desk trades CRYPTOCURRENCIES ONLY): the deterministic gate is THE
    # authority, so non-crypto is refused on EVERY path — a held non-crypto position force-CLOSED,
    # a fresh non-crypto OPEN is refused, and non-crypto triggers are purged (resting) / blocked
    # (arm-time). One shared counter + action-list across all four sites. Capability-guarded (a
    # legacy fake lacking is_crypto_raw skips, never production), FAIL-CLOSED, DIRECTION-symmetric
    # (keys on symbol only). Binance now lists tokenized stocks/commodities/metals/pre-IPO/indices
    # as USDT perps; the scout blocks them upstream, and these gate guards are the survival backstop
    # for any that arrive via a config.symbols pin, a --symbols override, or a Trader mis-name.
    _is_crypto_raw = getattr(exchange, "is_crypto_raw", None)
    auto_retired_noncrypto = 0
    noncrypto_actions: list = []

    def _noncrypto(raw: str) -> bool:  # True iff the gate must REFUSE this symbol (fail-closed)
        return _is_crypto_raw is not None and not _is_crypto_raw(raw)

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
        if _noncrypto(p.symbol):   # crypto-only: force-CLOSE a non-crypto holding (normal flow)
            force_close.add(p.symbol)
            new_positions.append(p)
            auto_retired_noncrypto += 1
            noncrypto_actions.append(
                f"force-CLOSE NON-CRYPTO holding {p.direction} {p.symbol} — desk is crypto-only")
            continue
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
                w = _noise_band_warning(p.symbol, float(ns), mark,
                                        _position_atr(ctx, p.symbol, now, settings.timeframe))
                if w:
                    reduce_warnings.append(w)
                survivor = survivor.model_copy(update={"stop": float(ns)})
                trailed += 1
            new_positions.append(survivor)
            continue
        if m and m.get("action") == "hold" and m.get("new_stop") is not None:
            ns = float(m["new_stop"])
            mark = ctx.prices.get(p.symbol)
            if _is_tighter_stop(p.direction, p.stop, ns, mark):
                w = _noise_band_warning(p.symbol, ns, mark,
                                        _position_atr(ctx, p.symbol, now, settings.timeframe))
                if w:
                    reduce_warnings.append(w)
                p = p.model_copy(update={"stop": ns})  # trail only; never loosen
                trailed += 1
        new_positions.append(p)
    positions = new_positions

    # --- Trigger orders: fire armed conditionals off the latest 4h bar, then they become NORMAL
    # proposals (at the trigger price) competing in the SAME gate. Held-symbol orders are skipped
    # (no stacking). On HALT a fired trigger is consumed but NOT opened (like a fresh open). ----
    held_symbols = {p.symbol for p in positions}
    # OI-confirmation source: only symbols with an armed require_oi_rising trigger need a (reactive,
    # completed-bar) OI read at fire time. When NO order opts in -> zero new OI calls (inert on the
    # execution hot path by default). Any feed error -> None -> the gate fail-closes (holds the
    # trigger, never a spurious fire).
    oi_gate_syms = {o.symbol for o in pending if getattr(o, "require_oi_rising", False)}
    bars_by_symbol: dict = {}
    oi_change_by_symbol: dict = {}
    # Current swing hi/lo per symbol (same swing_levels the brief feeds the team) — used both for
    # a newly-armed stop_entry's arm-time anchor AND to revalidate prior-armed ones for a swing that
    # has crossed PAST its level (the cy43 ETH inversion). Computed over completed bars (forming row
    # already dropped). A feed gap -> no entry -> fail-safe (no stamp, no auto-cancel).
    swings_by_symbol: dict = {}
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
        if raw in oi_gate_syms:   # completed-bar OI aligned to the same `now` the bar was read at
            oi_change_by_symbol[raw] = oi_change_for(exchange, uni, settings.timeframe, now)
        try:
            sh, sl = swing_levels(df)
            swings_by_symbol[raw] = (float(sh), float(sl))
        except Exception:  # noqa: BLE001 — feed gap -> no swing entry -> fail-safe (Rule 4)
            pass
    fired, expired, remaining = check_pending_orders(state_dir, bars_by_symbol, cycle_no,
                                                     held_symbols=held_symbols,
                                                     oi_change_by_symbol=oi_change_by_symbol)

    # AUTO-REVALIDATE armed stop_entry geometry: a trigger whose swing anchor crossed PAST its level
    # since it was armed (swing_low fell below a breakdown short, or swing_high rose over a breakout
    # long) would now fire MID-BOUNCE, not on a fresh break (the cy43 ETH case the RM caught by eye)
    # Auto-cancel it through the SAME flow as an explicit cancel — dropped from fired (never opened)
    # AND from remaining (not persisted) — so the team re-arms at the true level next cycle via the
    # flow (Rule 1, never a manual store edit). Only PRIOR-armed triggers are checked; this cycle's
    # new_triggers are placed against this swing and are fresh by construction. Symmetric+fail-safe.
    stale_orders, _ = revalidate_triggers(fired + remaining, swings_by_symbol)
    stale_ids = {o.id for o in stale_orders}
    stale_actions = []
    if stale_ids:
        fired = [o for o in fired if o.id not in stale_ids]
        remaining = [o for o in remaining if o.id not in stale_ids]
        for o in stale_orders:
            sh, sl = swings_by_symbol.get(o.symbol, (None, None))
            anchor = sl if o.direction == "short" else sh
            anchor_kind = "support" if o.direction == "short" else "resistance"
            stale_actions.append(
                f"auto-canceled STALE {o.direction} {o.kind} {o.symbol} @ {o.trigger_level} — "
                f"swing {anchor_kind} crossed to {anchor} (trigger stranded on the wrong side); "
                f"re-arm at the true level if still valid")

    # --- CRYPTO-ONLY purge (standing invariant, Rule 2): the desk trades CRYPTOCURRENCIES ONLY.
    # Binance now lists tokenized equities/commodities/metals/pre-IPO and crypto baskets as USDT
    # perps; the scout already blocks NEW non-crypto, and THIS retires any already resting in the
    # store. An armed trigger whose symbol is not a crypto market is dropped from BOTH `fired`
    # (never opens) AND `remaining` (not persisted) — the SAME flow as a stale/explicit cancel,
    # never a manual store edit. Symmetric (keys on symbol, never the side) and FAIL-CLOSED
    # (non_crypto_triggers retires anything not PROVEN crypto). Skipped only if the exchange lacks
    # the is_crypto_raw capability (legacy/fake wiring) — the real FuturesExchange always has it.
    if _is_crypto_raw is not None:
        crypto_map = {o.symbol: bool(_is_crypto_raw(o.symbol)) for o in (fired + remaining)}
        non_crypto, _ = non_crypto_triggers(fired + remaining, crypto_map)
        nc_ids = {o.id for o in non_crypto}
        if nc_ids:
            fired = [o for o in fired if o.id not in nc_ids]
            remaining = [o for o in remaining if o.id not in nc_ids]
            auto_retired_noncrypto += len(nc_ids)
            for o in non_crypto:
                noncrypto_actions.append(
                    f"auto-retired NON-CRYPTO {o.direction} {o.kind} {o.symbol} @ "
                    f"{o.trigger_level} — desk is crypto-only (tokenized stock/commodity/index "
                    f"is not a cryptocurrency); will not arm or fire")

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
            if _noncrypto(raw):   # crypto-only: REFUSE a non-crypto fresh open (gate is authority)
                auto_retired_noncrypto += 1
                noncrypto_actions.append(
                    f"refused NON-CRYPTO open {ap.direction} {raw} — desk is crypto-only")
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
    report["audit_dropped"] = len(audit_dropped)  # anti-hallucination drops (Pillar 4)
    if audit_dropped:
        report.setdefault("warnings", []).extend(
            f"AUDIT dropped {d.get('symbol')} ({d.get('_audit_reason')})" for d in audit_dropped)
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
    # Stamp the counter-regime SAFETY conversions too (same helper as Trader triggers) so a drifted
    # cr-safety trigger is auto-revalidatable in a later cycle — no provenance coverage gap.
    new_triggers = [_stamp_anchor_swing(po, swings_by_symbol) for po in cr_armed]
    cr_keys = {(o.symbol, o.direction, o.kind) for o in cr_armed}
    armed_collisions = 0
    if not halted:
        from futures_fund.pending_orders import PendingOrder
        for t in (triggers or []):
            try:
                fields = {**t, "created_cycle": cycle_no,
                          "expires_cycle": int(t.get("expires_cycle", cycle_no + 3))}
                po = _stamp_anchor_swing(PendingOrder.model_validate(fields), swings_by_symbol)
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
    # CP7 ARM-TIME crypto-only guard: never WRITE a non-crypto trigger to the store, even if the
    # Trader emitted one or a counter-regime conversion produced it (defense-in-depth behind the
    # scout). Symmetric + fail-closed, same capability/counter as the purge above.
    if _is_crypto_raw is not None and new_triggers:
        nc_new, kept_new = non_crypto_triggers(
            new_triggers, {o.symbol: bool(_is_crypto_raw(o.symbol)) for o in new_triggers})
        if nc_new:
            new_triggers = kept_new
            auto_retired_noncrypto += len(nc_new)
            for o in nc_new:
                noncrypto_actions.append(
                    f"blocked NON-CRYPTO arm {o.direction} {o.kind} {o.symbol} "
                    f"— desk is crypto-only")
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
    report["auto_canceled_stale"] = len(stale_ids)  # geometry-inverted triggers retired this cycle
    if stale_actions:                                # surface each so the team can re-arm (Rule 6)
        report.setdefault("actions", []).extend(stale_actions)
        report.setdefault("warnings", []).extend(stale_actions)
    report["auto_retired_noncrypto"] = auto_retired_noncrypto  # crypto-only refusals this cycle
    if noncrypto_actions:                            # surface each (Rule 6) — desk is crypto-only
        report.setdefault("actions", []).extend(noncrypto_actions)
        report.setdefault("warnings", []).extend(noncrypto_actions)
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

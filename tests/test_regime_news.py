"""Fix for the regime arbiter `news_flag_missing` degradation: the News ANALYST already emits a
per-symbol signals.risk_off_flag (Phase 4) that was consumed NOWHERE. aggregate_news_risk_off
folds it into the tri-state market-wide boolean the deterministic regime needs.

Tri-state contract:
  None  -> news genuinely UNAVAILABLE (feed warning OR no news reports) -> degraded, today's behavior
  False -> news pass ran, NO catalyst flagged -> news_term=0 but NOT degraded (the key tri-state fix)
  True  -> at least one news report flagged risk_off (user-chosen 'any 1 flag -> desk-wide')

The asymmetry is preserved end-to-end: news can only ADD risk-off pressure (<= 0.10 of score),
never unlock shorts or manufacture a confirmed risk-off.
"""
from datetime import UTC, datetime

from futures_fund.regime import classify_regime
from futures_fund.regime_news import aggregate_news_risk_off

NOW = datetime(2026, 3, 1, tzinfo=UTC)


def _news(symbol, flag):
    return {"agent": "news", "symbol": symbol, "stance": "bearish" if flag else "neutral",
            "confidence": 0.5, "signals": {"catalyst_count": 1 if flag else 0, "risk_off_flag": flag}}


def _other(symbol, agent="technical"):
    return {"agent": agent, "symbol": symbol, "stance": "neutral", "confidence": 0.5, "signals": {}}


# ---- tri-state: missing / unavailable -> None (today's degraded behavior preserved) ----

def test_no_news_reports_returns_none():
    # only non-news reports -> the news pass is absent -> degraded
    reps = [_other("BTCUSDT"), _other("ETHUSDT", "derivatives")]
    assert aggregate_news_risk_off(reps, briefs=[], warnings=[]) is None


def test_empty_reports_returns_none():
    assert aggregate_news_risk_off([], briefs=[], warnings=[]) is None


def test_feed_unavailable_warning_beats_any_flag():
    # a 'news feed' warning means the feed itself is down -> None even if a stale report is flagged
    reps = [_news("BTCUSDT", 1)]
    assert aggregate_news_risk_off(reps, briefs=[], warnings=["news feed unavailable — cap conviction on catalysts"]) is None


def test_feed_no_items_warning_returns_none():
    reps = [_news("ETHUSDT", 0)]
    assert aggregate_news_risk_off(reps, briefs=[], warnings=["news feed returned no items — treat catalysts as unknown"]) is None


# ---- tri-state: pass ran, no catalyst -> False (NOT None) — the core fix ----

def test_all_flags_zero_returns_false_not_none():
    reps = [_news("HYPEUSDT", 0), _news("BNBUSDT", 0), _news("SOLUSDT", 0)]
    out = aggregate_news_risk_off(reps, briefs=[], warnings=[])
    assert out is False  # explicitly False, not None


# ---- 'any 1 flag -> desk-wide True' (the user's chosen aggregation) ----

def test_btc_flag_true():
    reps = [_news("BTCUSDT", 1), _news("ETHUSDT", 0)]
    assert aggregate_news_risk_off(reps, briefs=[], warnings=[]) is True


def test_single_non_btc_major_flag_true():
    # under 'any 1 flag', even a single non-BTC major is desk-wide
    reps = [_news("ETHUSDT", 1), _news("BTCUSDT", 0)]
    assert aggregate_news_risk_off(reps, briefs=[], warnings=[]) is True


def test_single_non_major_flag_true():
    # an idiosyncratic non-major shock (hack/delisting) is desk-wide gap-risk
    reps = [_news("ZECUSDT", 1), _news("BTCUSDT", 0), _news("ETHUSDT", 0)]
    assert aggregate_news_risk_off(reps, briefs=[], warnings=[]) is True


def test_two_majors_flag_true():
    reps = [_news("ETHUSDT", 1), _news("SOLUSDT", 1), _news("BTCUSDT", 0)]
    assert aggregate_news_risk_off(reps, briefs=[], warnings=[]) is True


# ---- robustness: flag coercion never raises ----

def _flag(v):
    return [{"agent": "news", "symbol": "BTCUSDT", "signals": {"risk_off_flag": v}}]


def test_flag_coercion_truthiness():
    # int/bool 1/True set; LLM-authored string forms '1'/'true'/'yes' also set (the contract is an
    # int, but analyst_reports.json is untrusted JSON — missing a real shock is the unsafe failure)
    assert aggregate_news_risk_off([_news("BTCUSDT", True)], [], []) is True
    assert aggregate_news_risk_off([_news("BTCUSDT", 1)], [], []) is True
    assert aggregate_news_risk_off(_flag("1"), [], []) is True
    assert aggregate_news_risk_off(_flag("true"), [], []) is True
    assert aggregate_news_risk_off(_flag("TRUE"), [], []) is True
    assert aggregate_news_risk_off(_flag("yes"), [], []) is True
    # not set: 0 / None / absent / '0' / 'false' / '' / arbitrary string
    assert aggregate_news_risk_off([_news("BTCUSDT", 0)], [], []) is False
    assert aggregate_news_risk_off([_news("BTCUSDT", None)], [], []) is False
    assert aggregate_news_risk_off([{"agent": "news", "symbol": "BTCUSDT", "signals": {}}], [], []) is False
    assert aggregate_news_risk_off(_flag("0"), [], []) is False
    assert aggregate_news_risk_off(_flag("false"), [], []) is False
    assert aggregate_news_risk_off(_flag(""), [], []) is False
    assert aggregate_news_risk_off(_flag("maybe"), [], []) is False


def test_malformed_inputs_return_none_never_raise():
    assert aggregate_news_risk_off(None, None, None) is None
    assert aggregate_news_risk_off("garbage", briefs=[], warnings=[]) is None
    assert aggregate_news_risk_off([{"agent": "news"}, "junk", 42], briefs=[], warnings=[]) is False  # one valid news report, no flag
    assert aggregate_news_risk_off([{"not": "a report"}], briefs=[], warnings=[]) is None  # no news agent -> None


# ---- end-to-end: the tri-state actually changes the regime drivers correctly ----

def _uptrend_briefs():
    # all 5 majors present & UP (risk-on tape) so a news nudge cannot flip to confirmed
    return [{"exchange_id": m, "trend_direction": "up", "momentum_20": 0.05} for m in
            ("BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT")]


def test_false_clears_degraded_flag(tmp_path):
    mc = {"fear_greed": {"value": 55}}
    rs = classify_regime(tmp_path, mc, _uptrend_briefs(), NOW, cycle_no=1, news_risk_off=False)
    assert "news_flag_missing" not in rs.drivers["degraded"]
    assert rs.drivers["news_risk_off"] is False


def test_none_keeps_degraded_flag(tmp_path):
    mc = {"fear_greed": {"value": 55}}
    rs = classify_regime(tmp_path, mc, _uptrend_briefs(), NOW, cycle_no=1, news_risk_off=None)
    assert "news_flag_missing" in rs.drivers["degraded"]


def test_news_true_lowers_score_by_w_news(tmp_path):
    mc = {"fear_greed": {"value": 55}}
    base = classify_regime(tmp_path, mc, _uptrend_briefs(), NOW, cycle_no=1, news_risk_off=False)
    riskoff = classify_regime(tmp_path, mc, _uptrend_briefs(), NOW, cycle_no=1, news_risk_off=True)
    # news_term goes 0 -> -1 at weight 0.10
    assert round(base.score - riskoff.score, 4) == 0.10


def test_news_alone_cannot_manufacture_confirmed_on_clean_tape(tmp_path):
    # risk-on tape (majors up, F&G 60); even news=True must NOT confirm or unlock shorts
    mc = {"fear_greed": {"value": 60}}
    rs = classify_regime(tmp_path, mc, _uptrend_briefs(), NOW, cycle_no=1, news_risk_off=True)
    assert rs.confirmed is False


# ---- INVARIANT: the deterministic LABEL is news-blind (news never gates shorts) ----

from datetime import timedelta  # noqa: E402

from futures_fund.regime import append_regime_history  # noqa: E402

_CANDLE = timedelta(hours=4)


def _near_boundary_briefs():
    """A near-boundary bearish tape: only BTC down (breadth 0.2), BTC anchor down, F&G 20.
    score_core = 0.40*0.6 + 0.35*(-1) + 0.15*(-0.6) = 0.24 - 0.35 - 0.09 = -0.20 -> 'mixed'
    (above the -0.30 risk_off threshold). With the OLD news-in-label code, news=True (-0.10) would
    push the score to exactly -0.30 -> 'risk_off', and two such candles would manufacture a
    confirmed risk-off + unlock shorts. The fix makes the label news-blind, so it stays 'mixed'."""
    out = [{"exchange_id": "BTCUSDT", "trend_direction": "down", "momentum_20": -0.05}]
    out += [{"exchange_id": m, "trend_direction": "up", "momentum_20": 0.05}
            for m in ("ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT")]
    return out


def test_news_does_not_change_the_deterministic_label(tmp_path):
    mc = {"fear_greed": {"value": 20}}
    briefs = _near_boundary_briefs()
    blind = classify_regime(tmp_path, mc, briefs, NOW, cycle_no=1, news_risk_off=False)
    newsy = classify_regime(tmp_path, mc, briefs, NOW, cycle_no=1, news_risk_off=True)
    # the LABEL (what persistence/confirmed key on) is identical regardless of news
    assert blind.drivers["deterministic_regime"] == newsy.drivers["deterministic_regime"] == "mixed"
    # advisory score still reflects news (-0.20 -> -0.30) but the label did NOT flip
    assert round(blind.score - newsy.score, 4) == 0.10
    assert newsy.score == -0.30 and newsy.drivers["score_core"] == -0.20


def test_two_news_flagged_near_boundary_candles_never_confirm(tmp_path):
    # the exact scenario the review reproduced: a near-boundary tape + news=True on two consecutive
    # 4h candles must NEVER reach a confirmed risk_off via news (news stays out of the label).
    mc = {"fear_greed": {"value": 20}}
    briefs = _near_boundary_briefs()
    c1 = NOW
    rs1 = classify_regime(tmp_path, mc, briefs, c1, cycle_no=1, news_risk_off=True)
    append_regime_history(tmp_path, rs1)              # the gate persists each cycle's record
    rs2 = classify_regime(tmp_path, mc, briefs, c1 + _CANDLE, cycle_no=2, news_risk_off=True)
    append_regime_history(tmp_path, rs2)
    for rs in (rs1, rs2):
        assert rs.drivers["deterministic_regime"] == "mixed"
        assert rs.confirmed is False
    assert rs2.drivers["persistence_count"] == 0       # news-mixed candles never feed the K chain


def test_label_invariant_to_news_across_many_tapes(tmp_path):
    # PROPERTY: for ANY tape (breadth x btc x F&G), the deterministic LABEL — and thus `confirmed`
    # (advisory conviction strength) — must be identical whether news is None, False, or True. News
    # may only ever move the advisory `score`, never the label. This sweeps the whole input grid.
    dirs = ["up", "down"]
    for btc in dirs:
        for e in dirs:
            for b in dirs:
                for s in dirs:
                    for x in dirs:
                        for fng in (5, 25, 50, 75, 95):
                            briefs = [{"exchange_id": m, "trend_direction": d, "momentum_20": 0.0}
                                      for m, d in zip(("BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT"),
                                                      (btc, e, b, s, x))]
                            mc = {"fear_greed": {"value": fng}}
                            labels = {
                                n: classify_regime(tmp_path, mc, briefs, NOW, cycle_no=1,
                                                   news_risk_off=n).drivers["deterministic_regime"]
                                for n in (None, False, True)
                            }
                            assert labels[None] == labels[False] == labels[True], (btc, e, b, s, x, fng, labels)


def test_genuine_risk_off_still_confirms(tmp_path):
    # positive control: a HARD risk-off tape (all majors down, F&G 15) still confirms over K=2
    # WITHOUT any news — proving the fix only removed the NEWS path, not the deterministic chain.
    mc = {"fear_greed": {"value": 15}}
    briefs = [{"exchange_id": m, "trend_direction": "down", "momentum_20": -0.08} for m in
              ("BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT")]
    c1 = NOW
    rs1 = classify_regime(tmp_path, mc, briefs, c1, cycle_no=1, news_risk_off=None)
    append_regime_history(tmp_path, rs1)
    rs2 = classify_regime(tmp_path, mc, briefs, c1 + _CANDLE, cycle_no=2, news_risk_off=None)
    assert rs1.drivers["deterministic_regime"] == "risk_off" and rs1.confirmed is False  # 1 candle
    assert rs2.confirmed is True                                                          # K=2 met


# ---- Phase 4.6 reclassify_step (orchestration wiring) ----

from futures_fund.orchestration import _classify_regime_safe, reclassify_step  # noqa: E402


def _ctx(briefs, fng=55, warnings=None, cycle_no=1):
    """A preflight-shaped context: regime_state classified WITHOUT news (pass 1)."""
    mc = {"fear_greed": {"value": fng}, "warnings": warnings or []}
    rs = _classify_regime_safe("ignored", mc, briefs, NOW, cycle_no)
    return {"cycle": cycle_no, "market_context": mc, "briefs": briefs, "regime_state": rs}


def test_reclassify_folds_news_lowers_score_and_clears_degraded(tmp_path):
    ctx = _ctx(_uptrend_briefs())
    assert ctx["regime_state"]["drivers"]["news_risk_off"] is None  # pass 1 degraded (no news yet)
    reps = [_news("BTCUSDT", 1)] + [_news(m, 0) for m in ("ETHUSDT", "SOLUSDT")]
    rs2 = reclassify_step(tmp_path, ctx, reps)
    assert rs2["drivers"]["news_risk_off"] is True
    assert "news_flag_missing" not in rs2["drivers"]["degraded"]
    # news_term 0 -> -1 at weight 0.10
    assert round(ctx["regime_state"]["score"] - rs2["score"], 4) == 0.10


def test_reclassify_clean_pass_yields_false_not_degraded(tmp_path):
    ctx = _ctx(_uptrend_briefs())
    reps = [_news(m, 0) for m in ("BTCUSDT", "ETHUSDT", "SOLUSDT")]
    rs2 = reclassify_step(tmp_path, ctx, reps)
    assert rs2["drivers"]["news_risk_off"] is False
    assert "news_flag_missing" not in rs2["drivers"]["degraded"]
    assert rs2["score"] == ctx["regime_state"]["score"]  # no catalyst -> score unchanged


def test_reclassify_reuses_preflight_candle(tmp_path):
    ctx = _ctx(_uptrend_briefs())
    rs2 = reclassify_step(tmp_path, ctx, [_news("BTCUSDT", 1)])
    # same served candle as preflight -> persistence chain stays candle-consistent
    assert rs2["candle"] == ctx["regime_state"]["candle"]


def test_reclassify_junk_reports_degrade_to_none_not_crash(tmp_path):
    # aggregate_news_risk_off swallows non-list reports to None (degraded), so the regime still
    # classifies cleanly — equivalent to pass 1, not a crash.
    ctx = _ctx(_uptrend_briefs())
    rs2 = reclassify_step(tmp_path, ctx, "not-a-list")
    assert rs2["drivers"]["news_risk_off"] is None
    assert rs2["regime"] == ctx["regime_state"]["regime"]


def test_reclassify_failsafe_returns_prior_when_classify_raises(tmp_path):
    # the GENUINE except path: force classify_regime to raise over a non-trivial risk_off prior and
    # assert reclassify_step returns that prior VERBATIM (never downgrades, never raises).
    from unittest.mock import patch
    down = [{"exchange_id": m, "trend_direction": "down", "momentum_20": -0.06} for m in
            ("BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT")]
    ctx = _ctx(down, fng=25)
    prior = ctx["regime_state"]
    assert prior["drivers"]["deterministic_regime"] == "risk_off"
    with patch("futures_fund.regime.classify_regime", side_effect=RuntimeError("boom")):
        out = reclassify_step(tmp_path, ctx, [_news("BTCUSDT", 1)])
    assert out == prior  # returned unchanged — no downgrade, no exception escaped


def test_reclassify_none_context_returns_empty_prior(tmp_path):
    assert reclassify_step(tmp_path, None, [_news("BTCUSDT", 1)]) == {}


def test_reclassify_handles_missing_cycle_no(tmp_path):
    # context without a 'cycle' key must not make classify_regime raise (which would silently drop
    # the news fold); cycle_no falls back to the prior's, then 0.
    ctx = _ctx(_uptrend_briefs())
    ctx.pop("cycle")
    ctx["regime_state"]["cycle_no"] = 7
    rs2 = reclassify_step(tmp_path, ctx, [_news("BTCUSDT", 1)])
    assert rs2["drivers"]["news_risk_off"] is True   # the fold still happened
    assert rs2["cycle_no"] == 7                       # keyed off the prior's cycle_no


def test_reclassify_never_downgrades_riskoff_via_news(tmp_path):
    # a deepening tape (all majors down, F&G 25) is risk_off in pass 1; news can only deepen it
    down = [{"exchange_id": m, "trend_direction": "down", "momentum_20": -0.05} for m in
            ("BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT")]
    ctx = _ctx(down, fng=25)
    assert ctx["regime_state"]["drivers"]["deterministic_regime"] == "risk_off"
    rs2 = reclassify_step(tmp_path, ctx, [_news("BTCUSDT", 1)])
    assert rs2["drivers"]["deterministic_regime"] == "risk_off"  # never downgraded
    assert rs2["score"] <= ctx["regime_state"]["score"]          # only deeper


def test_news_nudge_leaving_mixed_does_not_count_as_riskoff(tmp_path):
    # a clean tape stays mixed even with news=True (score > -0.30) -> not a risk_off candle, so it
    # cannot feed the K=2 persistence chain (regime.py counts only deterministic_regime=='risk_off')
    ctx = _ctx(_uptrend_briefs())
    rs2 = reclassify_step(tmp_path, ctx, [_news("BTCUSDT", 1)])
    assert rs2["drivers"]["deterministic_regime"] != "risk_off"


# ---- _classify_regime_safe tri-state read (no regression + future-proof) ----

def test_classify_safe_missing_key_is_none(tmp_path):
    # today's market_context has NO news_risk_off key -> None (identical to prior behavior)
    rs = _classify_regime_safe(tmp_path, {"fear_greed": {"value": 55}}, _uptrend_briefs(), NOW, 1)
    assert rs["drivers"]["news_risk_off"] is None
    assert "news_flag_missing" in rs["drivers"]["degraded"]


def test_classify_safe_explicit_false_preserved(tmp_path):
    rs = _classify_regime_safe(tmp_path, {"fear_greed": {"value": 55}, "news_risk_off": False},
                               _uptrend_briefs(), NOW, 1)
    assert rs["drivers"]["news_risk_off"] is False
    assert "news_flag_missing" not in rs["drivers"]["degraded"]


def test_classify_safe_explicit_true_applies(tmp_path):
    base = _classify_regime_safe(tmp_path, {"fear_greed": {"value": 55}, "news_risk_off": False},
                                 _uptrend_briefs(), NOW, 1)
    rs = _classify_regime_safe(tmp_path, {"fear_greed": {"value": 55}, "news_risk_off": True},
                               _uptrend_briefs(), NOW, 1)
    assert round(base["score"] - rs["score"], 4) == 0.10


# ---- Phase 4.6 fail-loud guard: a SKIPPED reclassify must not pass the gate silently ----

from futures_fund.orchestration import reclassify_skipped  # noqa: E402


def test_reclassify_step_stamps_reclassified_marker(tmp_path):
    # reclassify_step marks that Phase 4.6 RAN, so the guard isn't fooled by a degraded-feed None
    ctx = _ctx(_uptrend_briefs())
    rs2 = reclassify_step(tmp_path, ctx, [_news("BTCUSDT", 1)])
    assert rs2["drivers"].get("reclassified") is True
    rs3 = reclassify_step(tmp_path, ctx, "not-a-list")   # degraded fold (None) but it RAN
    assert rs3["drivers"].get("reclassified") is True


def test_reclassify_skipped_true_when_news_ran_but_not_folded():
    # preflight-only regime_state (no marker, news_risk_off None) + news analysts ran -> SKIPPED
    rs = {"regime": "risk_off", "drivers": {"news_risk_off": None}}
    assert reclassify_skipped(rs, [_news("BTCUSDT", 1)]) is True


def test_reclassify_skipped_false_when_marker_present():
    # reclassify ran (marker) even if it folded to None (degraded feed) -> NOT skipped
    rs = {"regime": "risk_off", "drivers": {"news_risk_off": None, "reclassified": True}}
    assert reclassify_skipped(rs, [_news("BTCUSDT", 1)]) is False


def test_reclassify_skipped_false_when_news_folded(tmp_path):
    # news_risk_off in {True, False} -> reclassify ran (backward-compat for pre-marker contexts)
    rs = {"regime": "risk_off", "drivers": {"news_risk_off": True}}
    assert reclassify_skipped(rs, [_news("BTCUSDT", 1)]) is False


def test_reclassify_skipped_false_on_halt_no_news_reports():
    # HALT / stand-down: no analyst pass -> no news reports -> never block
    rs = {"regime": "risk_off", "drivers": {"news_risk_off": None}}
    assert reclassify_skipped(rs, []) is False


def test_reclassify_skipped_false_on_bad_inputs():
    assert reclassify_skipped(None, [_news("BTCUSDT", 1)]) is False
    assert reclassify_skipped({"drivers": {"news_risk_off": None}}, "not-a-list") is False

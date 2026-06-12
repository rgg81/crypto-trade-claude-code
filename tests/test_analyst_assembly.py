"""Canonical analyst-reports assembly + defensive normalization (cy77/cy78 backlog).

cy77 bit us twice: a hand-shaped DICT-of-agent-lists analyst_reports.json made `screen_step` return
EMPTY (it raised on non-list / found no `reports`) AND made `aggregate_news_risk_off` degrade the
news fold to None — both SILENTLY. The fix is two-pronged:
  - `assemble_analyst_reports(...)` builds the CANONICAL flat list (agent-tagged, conviction->
    confidence, thesis->key_points, the desk-wide risk_off_flag stamped on every news item,
    validated against the AnalystReport contract) so the orchestrator can't mis-shape it.
  - `normalize_reports(...)` makes the CONSUMERS tolerant: a flat list, a {"reports": [...]} wrap,
    OR a dict-of-agent-lists all flatten to the same agent-tagged flat list, recovering a desk-wide
    news flag — so a mis-shape never silently no-ops again.
"""
import pytest

from futures_fund.analyst_assembly import assemble_analyst_reports, normalize_reports
from futures_fund.contracts import AnalystReport


# --------------------------------------------------------------------------- assemble
def _tech():
    return [{"symbol": "BTCUSDT", "stance": "bullish", "confidence": 0.7, "thesis": "ADX trend"},
            {"symbol": "ETHUSDT", "stance": "neutral", "conviction": 0.2, "thesis": "chop"}]


def _deriv():
    return [{"symbol": "BTCUSDT", "stance": "bearish", "conviction": 0.5,
             "thesis": "crowded", "squeeze_risk": "high"}]


def _news():
    return [{"symbol": "BTCUSDT", "stance": "bearish", "conviction": 0.6, "thesis": "ETF outflows"}]


def _sent():
    return [{"symbol": "BTCUSDT", "stance": "neutral", "confidence": 0.4,
             "thesis": "wall of worry", "contrarian_note": "F&G 12"}]


def test_assemble_produces_canonical_validatable_flat_list():
    out = assemble_analyst_reports(_tech(), _deriv(), _news(), _sent(), news_risk_off_flag=False)
    assert isinstance(out, list) and len(out) == 5          # 2 tech + 1 deriv + 1 news + 1 sent
    # every item validates against the contract
    for r in out:
        AnalystReport.model_validate(r)
    agents = sorted({r["agent"] for r in out})
    assert agents == ["derivatives", "news", "sentiment", "technical"]


def test_assemble_maps_conviction_to_confidence_and_thesis_to_key_points():
    out = assemble_analyst_reports(_tech(), _deriv(), _news(), _sent(), news_risk_off_flag=False)
    eth = next(r for r in out if r["symbol"] == "ETHUSDT")
    assert eth["confidence"] == 0.2                          # conviction -> confidence
    assert eth["key_points"] == ["chop"]                     # thesis -> key_points


def test_assemble_stamps_risk_off_flag_on_every_news_item():
    out = assemble_analyst_reports(_tech(), _deriv(), _news(), _sent(), news_risk_off_flag=True)
    news = [r for r in out if r["agent"] == "news"]
    assert news and all(r["signals"]["risk_off_flag"] is True for r in news)
    # non-news items must NOT carry the flag
    assert all("risk_off_flag" not in (r.get("signals") or {})
               for r in out if r["agent"] != "news")


def test_assemble_normalizes_long_short_stance_to_bullish_bearish():
    out = assemble_analyst_reports(
        [{"symbol": "BTCUSDT", "stance": "long", "confidence": 0.6}],
        [], [{"symbol": "BTCUSDT", "stance": "short", "confidence": 0.5}],
        [], news_risk_off_flag=False)
    assert next(r for r in out if r["agent"] == "technical")["stance"] == "bullish"
    assert next(r for r in out if r["agent"] == "news")["stance"] == "bearish"


def test_assemble_raises_loudly_on_uncoercible_item():
    # a missing symbol cannot be coerced -> fail LOUD (don't silently drop the orchestrator's bug)
    with pytest.raises(ValueError):
        assemble_analyst_reports([{"stance": "bullish", "confidence": 0.5}], [], [], [],
                                 news_risk_off_flag=False)


# --------------------------------------------------------------------------- normalize (defensive)
def test_normalize_passes_through_flat_list():
    flat = [{"agent": "technical", "symbol": "BTCUSDT", "stance": "bullish", "confidence": 0.5}]
    assert normalize_reports(flat) == flat


def test_normalize_unwraps_reports_key():
    flat = [{"agent": "news", "symbol": "BTCUSDT", "stance": "bearish", "confidence": 0.5}]
    assert normalize_reports({"reports": flat}) == flat


def test_normalize_flattens_dict_of_agent_lists_and_tags_agent():
    # the cy77 shape: a dict keyed by agent name with per-agent lists, items lacking `agent`
    dicty = {"technical": [{"symbol": "BTCUSDT", "stance": "bullish", "confidence": 0.7}],
             "news": [{"symbol": "BTCUSDT", "stance": "bearish", "confidence": 0.6}]}
    out = normalize_reports(dicty)
    assert {r["agent"] for r in out} == {"technical", "news"}
    assert len(out) == 2


def test_normalize_recovers_desk_wide_news_flag_for_dict_shape():
    # a dict-of-lists with the flag at top level (not on each news item) still folds correctly
    dicty = {"news": [{"symbol": "BTCUSDT", "stance": "bearish", "confidence": 0.6}],
             "news_risk_off": True}
    out = normalize_reports(dicty)
    news = [r for r in out if r["agent"] == "news"]
    assert news and news[0]["signals"]["risk_off_flag"] is True


def test_normalize_garbage_is_empty_not_raise():
    assert normalize_reports(None) == []
    assert normalize_reports(42) == []


# ----------------------------------------------------------- review fixes (adversarial findings)
def test_unrecognized_stance_defaults_to_neutral_never_a_side():
    # HARD RULE 5: an unknown stance must NOT inject a directional bias (default-to-a-side bug).
    from futures_fund.analyst_assembly import _norm_stance
    assert _norm_stance("wobbly") == "neutral"
    assert _norm_stance("") == "neutral"
    assert _norm_stance(None) == "neutral"
    assert _norm_stance("LONG") == "bullish" and _norm_stance("Short") == "bearish"


def test_present_but_null_confidence_does_not_shadow_conviction():
    # finding[0]: {"conviction": 0.6, "confidence": null} must resolve to 0.6, not collapse to 0.0
    from futures_fund.analyst_assembly import _norm_confidence
    assert _norm_confidence({"conviction": 0.6, "confidence": None}) == 0.6
    assert _norm_confidence({"conviction": None, "confidence": None}) == 0.0
    out = assemble_analyst_reports([{"symbol": "BTCUSDT", "stance": "bullish",
                                     "conviction": 0.6, "confidence": None}], [], [], [],
                                   news_risk_off_flag=False)
    assert out[0]["confidence"] == 0.6


def test_screen_survives_null_confidence_shadowing_conviction():
    # finding[0] end-to-end: the cy77 footgun (high-conviction symbol -> EMPTY) must NOT recur
    from futures_fund.orchestration import screen_step
    dicty = {"technical": [{"symbol": "BTCUSDT", "stance": "bullish", "conviction": 0.8,
                            "confidence": None}],
             "derivatives": [{"symbol": "BTCUSDT", "stance": "bullish", "conviction": 0.6,
                              "confidence": None}]}
    assert screen_step(dicty, top_n=5) == ["BTCUSDT"]


def test_canonicalize_lenient_skips_uncoercible_and_canonicalizes():
    from futures_fund.analyst_assembly import canonicalize_lenient
    out = canonicalize_lenient([{"symbol": "BTCUSDT", "stance": "long", "conviction": 0.6},
                                {"stance": "bullish", "confidence": 0.5}])   # 2nd has no symbol
    assert len(out) == 1                          # un-coercible item SKIPPED, not raised
    assert out[0]["confidence"] == 0.6 and out[0]["stance"] == "bullish"     # conviction + synonym
    assert out[0]["agent"] == "technical"         # untagged flat-list item gets a default tag


def test_screen_drops_all_neutral_symbol_via_cy77_path():
    # invariant 'screen drops zero-conviction symbols' must hold through the NEW canonicalize path
    from futures_fund.orchestration import screen_step
    dicty = {"technical": [{"symbol": "NEUUSDT", "stance": "neutral", "conviction": 0.9},
                           {"symbol": "BTCUSDT", "stance": "bullish", "conviction": 0.4}],
             "derivatives": [{"symbol": "NEUUSDT", "stance": "neutral", "conviction": 0.9}]}
    out = screen_step(dicty, top_n=5)
    assert "NEUUSDT" not in out and out == ["BTCUSDT"]   # all-neutral dropped


def test_desk_wide_true_flag_dominates_an_item_false():
    # finding[12]/[10]: a True desk-wide flag must NOT be silently dropped by an item's own False
    out = assemble_analyst_reports([], [], [{"symbol": "BTCUSDT", "stance": "neutral",
                                             "confidence": 0.2,
                                             "signals": {"risk_off_flag": False}}],
                                   [], news_risk_off_flag=True)
    assert out[0]["signals"]["risk_off_flag"] is True
    from futures_fund.regime_news import aggregate_news_risk_off
    conflict = {"news": [{"symbol": "BTCUSDT", "stance": "neutral", "conviction": 0.2,
                          "signals": {"risk_off_flag": False}}], "news_risk_off": True}
    assert aggregate_news_risk_off(conflict) is True
    # mirror: a True ITEM flag survives a desk-wide False
    out2 = assemble_analyst_reports([], [], [{"symbol": "BTCUSDT", "stance": "neutral",
                                              "confidence": 0.2,
                                              "signals": {"risk_off_flag": True}}],
                                    [], news_risk_off_flag=False)
    assert out2[0]["signals"]["risk_off_flag"] is True


def test_reports_key_does_not_shadow_agent_keyed_data():
    # finding[8]: a dict carrying BOTH agent lists AND a stray "reports" key keeps the agent data
    mixed = {"technical": [{"symbol": "BTCUSDT", "stance": "bullish", "confidence": 0.5}],
             "reports": [{"symbol": "ZZZUSDT", "stance": "bearish", "confidence": 0.9}]}
    out = normalize_reports(mixed)
    assert any(r["symbol"] == "BTCUSDT" for r in out)        # agent data preserved
    assert all(r.get("symbol") != "ZZZUSDT" for r in out)    # stray "reports" not taken as wrapper


def test_extra_keys_fold_into_signals():
    # finding[13]: holding_intact / squeeze_risk / contrarian_note ride into signals
    out = assemble_analyst_reports(
        [], [{"symbol": "BTCUSDT", "stance": "bearish", "confidence": 0.5, "squeeze_risk": "high"}],
        [], [{"symbol": "BTCUSDT", "stance": "neutral", "confidence": 0.3,
              "holding_intact": True}], news_risk_off_flag=False)
    deriv = next(r for r in out if r["agent"] == "derivatives")
    sent = next(r for r in out if r["agent"] == "sentiment")
    assert deriv["signals"]["squeeze_risk"] == "high"
    assert sent["signals"]["holding_intact"] is True


# ----------------------------------------------------------- consumer-level recovery (the real bug)
def test_screen_step_recovers_a_cy77_dict_of_lists():
    # cy77 footgun: a hand-shaped dict-of-agent-lists with `conviction` (not `confidence`). Old
    # screen_step returned EMPTY silently; it must now rank the conviction-weighted symbols.
    from futures_fund.orchestration import screen_step
    dicty = {
        "technical": [{"symbol": "BTCUSDT", "stance": "bullish", "conviction": 0.8},
                      {"symbol": "ETHUSDT", "stance": "neutral", "conviction": 0.1}],
        "derivatives": [{"symbol": "BTCUSDT", "stance": "bullish", "conviction": 0.6}],
        "news": [{"symbol": "ETHUSDT", "stance": "bearish", "conviction": 0.7}],
        "sentiment": [{"symbol": "ETHUSDT", "stance": "bearish", "conviction": 0.5}],
    }
    out = screen_step(dicty, top_n=5)
    assert "BTCUSDT" in out and "ETHUSDT" in out      # not silently empty
    assert out[0] == "BTCUSDT"   # BTC net (0.8+0.6)*2 agents = 2.8 > ETH (0.7+0.5)*2 = 2.4


def test_news_fold_recovers_a_cy77_dict_of_lists():
    # the news fold must read the desk-wide flag even from a dict-of-lists with a top-level flag
    from futures_fund.regime_news import aggregate_news_risk_off
    flagged = {"news": [{"symbol": "BTCUSDT", "stance": "bearish", "conviction": 0.6}],
               "news_risk_off": True}
    assert aggregate_news_risk_off(flagged) is True       # was silently None before
    clean = {"news": [{"symbol": "BTCUSDT", "stance": "neutral", "conviction": 0.2,
                       "signals": {"risk_off_flag": False}}]}
    assert aggregate_news_risk_off(clean) is False        # pass ran, no shock (not degraded None)

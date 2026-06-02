"""E — ship gate for the conservatism fix. The cure must not reinstate the disease: the rebalanced
signals must STILL block the desk's documented losers (5 shorts faded into risk-on; one chased
crowded-LONG BNB breakout), and the under-deployment accelerator must be structurally incapable of
amplifying the early over-trading (it only fires when FLAT + idle). LLM debates aren't
deterministically replayable, so we assert the signals that drive those decisions."""
from datetime import UTC, datetime, timedelta

from futures_fund.lessons import append_lesson, retrieve_lessons


def _corpus(tmp):
    """A representative post-fix corpus: the validated risk brakes + the seeded enabling rule."""
    now = datetime(2026, 5, 31, tzinfo=UTC)
    L = [
        # the desk's single most data-validated brake (5 of 6 losses were shorts into risk-on)
        dict(text="Do NOT open directional SHORTS on majors/major-adjacent in an established risk-on "
                  "rotation.", polarity="restrictive", regime=None, tags=["short", "risk_on", "trend"],
             importance=8),
        # anti-chasing brake WITH the crowded-short carve-out
        dict(text="Do NOT enter a breakout long after an extended run (chasing). CARVE-OUT: does NOT "
                  "apply to a crowded-short squeeze-long (L/S<1 + negative funding) — that is the "
                  "desk's edge, not a chase; the brake is for chasing strength into a crowded-LONG "
                  "book.", polarity="restrictive", regime="trend", tags=["breakout", "chasing", "long"],
             importance=7),
        dict(text="Cut/avoid a SHORT with negative funding + crowded shorts (L/S<0.85).",
             polarity="restrictive", regime=None, tags=["short", "funding"], importance=6),
        dict(text="Close a TIME-BOUNDED thesis on schedule.", polarity="process", regime=None,
             tags=["exit"], importance=8),
        # the seeded enabling edge rule
        dict(text="DO take the LONG on a crowded-short squeeze (L/S<~0.85 + negative funding) in an "
                  "up/recovering trend — the desk's only proven edge. Size normally.",
             polarity="enabling", regime=None, tags=["squeeze", "long", "funding", "edge"],
             importance=8),
    ]
    for i, lz in enumerate(L):
        append_lesson(tmp, lz, ts=now - timedelta(hours=i + 1))
    return now


def test_dont_short_in_riskon_still_retrieved(tmp_path):
    """The quota must NOT cap away the desk's most-validated brake when a short is on the table."""
    now = _corpus(tmp_path)
    got = retrieve_lessons(tmp_path, now=now, regime="high_vol_trend",
                           query_tags=["short", "risk_on", "trend"], k=6)
    assert any("do not open directional shorts" in lz.text.lower() for lz in got), \
        "the don't-short-in-risk-on brake was suppressed — would re-bless the 5 losing shorts"


def test_enabling_rule_is_long_only_does_not_bless_shorts(tmp_path):
    """The accelerator lesson must require L/S<0.85 + neg funding LONG — it can't justify the
    losing major-shorts."""
    now = _corpus(tmp_path)
    enabling = [lz for lz in retrieve_lessons(tmp_path, now=now, regime="high_vol_trend",
                                              query_tags=["squeeze", "long"], k=6)
                if lz.polarity == "enabling"]
    assert enabling, "enabling edge rule missing"
    t = enabling[0].text.lower()
    assert "long" in t and ("l/s<" in t or "crowded-short" in t) and "negative funding" in t
    assert "short" not in t.split("crowded-short")[0]  # not a license to short


def test_anti_chasing_carveout_does_not_exempt_crowded_long_chase(tmp_path):
    """The BNB loss was a crowded-LONG chase. The carve-out only exempts crowded-SHORT squeezes, so
    the anti-chasing brake must still apply to a crowded-long breakout chase."""
    now = _corpus(tmp_path)
    got = retrieve_lessons(tmp_path, now=now, regime="trend",
                           query_tags=["breakout", "chasing", "long"], k=6)
    chase = next((lz for lz in got if "chasing" in lz.text.lower()), None)
    assert chase is not None, "anti-chasing brake missing"
    t = chase.text.lower()
    # carve-out is explicitly scoped to crowded-SHORT (L/S<1 + neg funding), NOT crowded-long
    assert "carve-out" in t and "crowded-short" in t and "l/s<1" in t
    assert "crowded-long" in t  # names that the brake still fires on crowded-long chases


def test_accelerator_cannot_amplify_overtrading(tmp_path):
    """During the early over-trading (cycles 1-6 held positions / opened trades), the under-
    deployment accelerator must be SILENT — it requires FLAT + zero recent opens, so it can never
    have pushed MORE trades while the desk was already over-trading."""
    from tests.test_scorecard import _seed_idle_tradeable
    from futures_fund.scorecard import build_scorecard
    # mirror an actively-trading cycle: holding a position
    s, m = tmp_path / "s", tmp_path / "m"
    _seed_idle_tradeable(s, m, positions=[{"symbol": "ETHUSDT"}], opened_recent=1)
    sc = build_scorecard(s, m, monthly_target=0.05)
    assert not any("under-deployed" in w for w in sc["warnings"])

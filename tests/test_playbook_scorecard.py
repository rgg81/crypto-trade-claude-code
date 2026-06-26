"""Locked-invariant suite for the playbook scorecard (Learning Direction A). These tests ARE the
acceptance criteria from the adversarial design review: outcome-invariance, mirror/long-short
symmetry, the expectancy-not-hit-rate guard, the hard min-n gate, the two-sided/never-only-caution
contract, fail-closed regime join, cold-start abstain, fail-safe, and the protected boundary."""
import ast
import inspect
import json

import pytest

from futures_fund import playbook_scorecard as P


def _rec(direction="long", entry=100.0, stop=95.0, size=2.0, pnl=20.0, cycle=20,
         fees=0.0, funding_paid=0.0, symbol="BTCUSDT"):
    return {"direction": direction, "entry": entry, "stop": stop, "size": size,
            "realized_pnl": pnl, "cycle": cycle, "fees": fees, "funding_paid": funding_paid,
            "symbol": symbol, "take_profit": [entry + (entry - stop) * 2]}


# --------------------------------------------------------------------------- R reconstruction
def test_reconstruct_r_gross_and_net():
    g, n = P.reconstruct_r(_rec(entry=100, stop=95, size=2, pnl=20, fees=1.0, funding_paid=0.5))
    assert n == pytest.approx(2.0)          # 20 / (2*5)
    assert g == pytest.approx(2.15)         # (20+1+0.5)/10 — fee/funding added back


def test_reconstruct_r_drops_bad_geometry():
    assert P.reconstruct_r(_rec(entry=100, stop=100)) == (None, None)   # zero risk
    assert P.reconstruct_r(_rec(pnl=None)) == (None, None)              # un-closed
    assert P.reconstruct_r(_rec(size=float("nan"))) == (None, None)     # non-finite


# --------------------------------------------------------------------------- classifier invariants
def test_classify_is_outcome_blind_by_signature():
    # the classifier must accept ONLY (rec, regime_label) — no outcome field in its signature
    params = list(inspect.signature(P.classify_setup).parameters)
    assert params == ["rec", "regime_label"]


def test_classify_outcome_invariance():
    # scrambling the outcome must NOT change the classification
    base = _rec(direction="short", pnl=50.0)
    flipped = {**base, "realized_pnl": -999.0}
    assert P.classify_setup(base, "risk_off") == P.classify_setup(flipped, "risk_off")


def test_classify_regime_alignment_is_mirror_symmetric():
    # long+risk_on and short+risk_off are BOTH 'with'; opposites BOTH 'counter' (no L/S bias)
    assert P.classify_setup(_rec(direction="long"), "risk_on")["regime_alignment"] == "with"
    assert P.classify_setup(_rec(direction="short"), "risk_off")["regime_alignment"] == "with"
    assert P.classify_setup(_rec(direction="long"), "risk_off")["regime_alignment"] == "counter"
    assert P.classify_setup(_rec(direction="short"), "risk_on")["regime_alignment"] == "counter"
    assert P.classify_setup(_rec(direction="long"), "mixed")["regime_alignment"] == "neutral"
    assert P.classify_setup(_rec(direction="short"), None)["regime_alignment"] is None  # no regime


# ----------------------------------------------------------------------- regime join (fail-closed)
def test_load_regime_by_cycle_binds_right_key_and_tolerates_torn_lines(tmp_path):
    (tmp_path / "regime_history.jsonl").write_text(
        json.dumps({"cycle_no": 16, "deterministic_regime": "risk_off", "regime": "risk_off",
                    "label": None}) + "\n"
        + "{ torn line\n"
        + json.dumps({"cycle_no": 17, "regime": "risk_on"}) + "\n")   # fallback to `regime`
    rbc = P.load_regime_by_cycle(tmp_path)
    assert rbc == {16: "risk_off", 17: "risk_on"}   # `label` (null) ignored; torn line skipped


def test_unjoinable_trade_excluded_from_regime_buckets_not_coerced():
    # a trade whose cycle has NO regime row is counted in n_unjoinable and kept OUT of regime cells
    decs = [_rec(direction="short", cycle=999, pnl=10.0)]   # cycle 999 absent from the map
    agg = P.aggregate_playbook(decs, {16: "risk_off"})
    assert agg["coverage"]["n_unjoinable"] == 1
    assert agg["regime_buckets"] == {}                      # never an "unknown" bucket
    assert "short" in agg["side_buckets"]                   # still in the regime-pooled side bucket


# --------------------------------------------------------------------------- statistics
def test_wilson_interval_basic():
    lo, hi = P.wilson_interval(5, 10)
    assert 0.0 <= lo < 0.5 < hi <= 1.0
    assert P.wilson_interval(0, 0) == (0.0, 1.0)


def test_beta_binomial_shrinks_toward_base_rate():
    # a 2/3 bucket with base rate 0.4 must read NEAR the prior, not 0.67
    s = P.beta_binomial_shrink(2, 3, base_rate=0.4, m=8.0)
    assert 0.40 < s < 0.55


def test_mean_ci_single_and_empty():
    assert P.mean_ci([]) == (0.0, 0.0, 0.0)
    assert P.mean_ci([1.5]) == (1.5, 1.5, 1.5)


# --------------------------------------------------------------------------- aggregation symmetry
def _symmetric_book():
    # N longs in risk_on (with-regime) and N shorts in risk_off (with-regime) with the SAME R-set
    rs = [3.0, -1.0, 2.0, -1.0, 1.5, -1.0, 2.5, -1.0, 1.0]   # 9 trades; risk=10 so net_r==pnl/10
    longs = [_rec(direction="long", cycle=20, pnl=r * 10) for r in rs]
    shorts = [_rec(direction="short", cycle=21, pnl=r * 10) for r in rs]
    return longs + shorts


def test_long_short_R_symmetry():
    agg = P.aggregate_playbook(_symmetric_book(), {20: "risk_on", 21: "risk_off"})
    lo, sh = agg["side_buckets"]["long"], agg["side_buckets"]["short"]
    assert lo["net_r_mean"] == sh["net_r_mean"]            # identical expectancy
    assert lo["hit_rate"] == sh["hit_rate"]
    assert lo["net_r_ci"] == sh["net_r_ci"]
    # mirror regime cells: long/with-regime == short/with-regime
    assert agg["regime_buckets"]["long/with-regime"]["net_r_mean"] == \
           agg["regime_buckets"]["short/with-regime"]["net_r_mean"]


# --------------------------------------------------------------------------- min-n gate
def test_no_directive_or_number_below_min_n():
    decs = [_rec(direction="long", cycle=20, pnl=30.0) for _ in range(3)]   # n=3 < 8 AND < 4
    agg = P.aggregate_playbook(decs, {20: "risk_on"})
    out = P.format_playbook_advisory(agg, total_closed=3, dormancy_n=0)
    assert "insufficient sample" in out
    assert "net " not in out.split("long book")[1].split("\n")[0]   # no R number on the long line


# ----------------------------------------------------- regime cells surface at the lower floor (#5)
def test_regime_cell_surfaces_at_lower_floor_as_leaning_not_working():
    # 6 short/with-regime trades (risk_off), strongly positive but n in [REGIME_MIN_N(4), MIN_N(8)):
    # the cell must SURFACE (not "insufficient", not pooled) as a SOFT "leaning favorable" read, and
    # must NOT be minted as the hard "WORKING — favor it" / EDGE WORKING (those still need n>=8).
    rs = [2.0, 1.5, 1.8, 1.4, 2.2, 1.6]                      # n=6, all positive, CI clear of 0
    decs = [_rec(direction="short", cycle=21, pnl=r * 10) for r in rs]
    agg = P.aggregate_playbook(decs, {21: "risk_off"})
    assert "short/with-regime" in agg["regime_buckets"]      # surfaced at n=6 (pooled away at n>=8)
    b = agg["regime_buckets"]["short/with-regime"]
    assert b["n"] == 6 and b["direction_sign"] == "pos"
    assert b.get("significant") is False                     # n<8 -> never significant
    out = P.format_playbook_advisory(agg, total_closed=6, dormancy_n=0)
    assert "short/with-regime" in out and "leaning favorable" in out
    assert "WORKING — favor it" not in out                   # no hard WORKING verdict at n=6
    assert "EDGE WORKING" not in out                         # no hard callout at n=6
    assert "LEANING (thin sample, soft)" in out              # the soft concentrate-here line


def test_regime_cell_below_regime_floor_still_pooled():
    # n=3 < REGIME_MIN_N(4) -> still pooled away, not surfaced (the floor is 4, not 1).
    decs = [_rec(direction="short", cycle=21, pnl=20.0) for _ in range(3)]
    agg = P.aggregate_playbook(decs, {21: "risk_off"})
    assert "short/with-regime" not in agg["regime_buckets"]


def test_regime_significance_still_needs_min_n_not_regime_floor():
    # a thin (n=6) regime cell is NOT in the Holm significance family even if its raw p is tiny:
    # significance (the hard WORKING bar) still requires n>=MIN_N(8).
    rs = [2.0, 2.0, 2.0, 2.0, 2.0, 2.0]                      # n=6, zero-dispersion-ish, strong +
    decs = [_rec(direction="short", cycle=21, pnl=r * 10) for r in rs]
    agg = P.aggregate_playbook(decs, {21: "risk_off"})
    assert agg["regime_buckets"]["short/with-regime"].get("significant") is False


# ------------------------------------------------------------------------ expectancy-not-hit-rate
def test_low_hit_positive_expectancy_is_never_caution():
    # 8 trades, hit 25% (2/8) but +0.5R expectancy -> favorable/inconclusive, NEVER caution
    rs = [5.0, 5.0, -1.0, -1.0, -1.0, -1.0, -1.0, -1.0]   # mean +0.5R, 2 wins/8
    b = P._bucket_stats([r for r in rs], [r for r in rs], wins=2, base_rate=0.4)
    v = P._verdict(b)
    assert "size down" not in v and "underperforming" not in v


def test_high_hit_negative_expectancy_is_never_favorable():
    # 8 trades, hit 75% (6/8) but NEGATIVE expectancy (small wins, huge losses) -> never 'favorable'
    rs = [0.2, 0.2, 0.2, 0.2, 0.2, 0.2, -5.0, -5.0]       # mean negative, 6 wins/8
    b = P._bucket_stats(rs, rs, wins=6, base_rate=0.4)
    v = P._verdict(b)
    assert "favor" not in v.lower()


def test_tiny_positive_conclusive_bucket_never_reads_caution():
    # DEFECT-1 regression: a conclusively-positive bucket whose mean ROUNDS to 0.000 must NOT fall
    # through to caution. The verdict keys off the unrounded CI sign, not the rounded mean.
    rs = [0.0004] * 8                                     # CI conclusively > 0; round(mean,3)==0.0
    b = P._bucket_stats(rs, rs, wins=8, base_rate=0.5)
    assert b["net_r_mean"] == 0.0 and b["direction_sign"] == "pos"   # rounds to 0, conclusively +
    v = P._verdict(b)
    assert "size down" not in v and "underperforming" not in v       # never caution


# ----------------------------------------------------------------------- two-sided / anti-ratchet
def test_never_only_caution_surfaces_working_edge():
    # one clearly-working bucket (long) + one weak bucket (short): advisory MUST surface the edge
    longs = [_rec(direction="long", cycle=20, pnl=30.0) for _ in range(8)]   # +3R x8 -> conclusive
    shorts = [_rec(direction="short", cycle=21, pnl=-20.0) for _ in range(8)]  # -2R x8
    agg = P.aggregate_playbook(longs + shorts, {20: "risk_on", 21: "risk_off"})
    out = P.format_playbook_advisory(agg, book_flat=False, total_closed=16, dormancy_n=0)
    assert "EDGE WORKING" in out
    assert "long" in out


def test_cautions_self_silence_when_flat():
    longs = [_rec(direction="long", cycle=20, pnl=30.0) for _ in range(8)]
    shorts = [_rec(direction="short", cycle=21, pnl=-20.0) for _ in range(8)]
    agg = P.aggregate_playbook(longs + shorts, {20: "risk_on", 21: "risk_off"})
    flat = P.format_playbook_advisory(agg, book_flat=True, total_closed=16, dormancy_n=0)
    assert "SIZE-DOWN" not in flat            # summary caution silenced when flat
    assert "size down" not in flat.lower()    # AND every per-line caution silenced (OBS-B)
    assert "EDGE WORKING" in flat             # but a working edge is STILL surfaced when flat


# --------------------------------------------------------------------------- cold-start / fail-safe
def test_cold_start_abstains_with_no_numbers():
    agg = P.aggregate_playbook([], {})
    out = P.format_playbook_advisory(agg, total_closed=0)
    assert "no record yet" in out
    thin = P.aggregate_playbook([_rec(cycle=20, pnl=10.0)], {20: "risk_on"})
    out2 = P.format_playbook_advisory(thin, total_closed=1, dormancy_n=60)
    assert "cold-starting" in out2 and "ABSTAINS" in out2


def test_top_level_is_fail_safe_on_corrupt_data(tmp_path, monkeypatch):
    # any read error -> a benign abstaining line, never a raise into the cycle
    monkeypatch.setattr(P, "read_all_decisions", lambda *_a, **_k: (_ for _ in ()).throw(OSError()))
    out = P.playbook_advisory(tmp_path / "m", tmp_path / "s")
    assert "PLAYBOOK" in out and "abstain" in out.lower()


# ------------------------------------------------------------------------ protected-boundary guard
def test_module_touches_no_protected_module_and_never_writes():
    PROTECTED = {"risk_gate", "executor", "exits", "consolidation", "policy", "liquidation",
                 "sizing", "cycle"}
    src = inspect.getsource(P)
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            mod = (node.module or "") if isinstance(node, ast.ImportFrom) else ""
            names = [mod] + [a.name for a in getattr(node, "names", [])]
            for nm in names:
                tail = nm.split(".")[-1]
                assert tail not in PROTECTED, f"playbook imports protected module {nm}"
    # no write/dynamic-import surface (OBS-C: future-proof, not just substring `open(...,'w')`)
    for banned in ("save_", "append_decision", "importlib", "__import__", "write_text",
                   "write_bytes", "os.write", "json.dump", "patch_outcome"):
        assert banned not in src, f"playbook uses a banned write/dynamic-import surface: {banned}"
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
            continue
        if node.func.id != "open":
            continue
        has_mode = len(node.args) > 1 and isinstance(node.args[1], ast.Constant)
        mode = str(node.args[1].value) if has_mode else "r"
        assert "w" not in mode and "a" not in mode, "playbook opens a file for writing"

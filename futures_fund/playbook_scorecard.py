"""Playbook scorecard (Learning Direction A) — the desk's quantitative memory of WHAT HAS WORKED.

A NON-PROTECTED, READ-ONLY, ADVISORY aggregator. It reads only the realized decision journal
(`read_all_decisions`) + `state/regime_history.jsonl`, classifies each closed trade with an
OUTCOME-BLIND, side-agnostic classifier, reconstructs its R-multiple, and reports per-bucket
EXPECTANCY (avg R) with honest small-sample statistics. The string it produces is injected ONLY into
the Research Manager prompt — exactly the way the regime read and the lessons corpus already are. It
NEVER touches a protected module, never writes state, and emits no sizing/gate directive.

Design invariants (from the 15-agent adversarial design review, 2026-06-12):
- EXPECTANCY-LED, never hit-rate-led: the gate enforces RR>=1.6, so the desk's best (with-regime
  trend) trades are sub-50%-hit BY DESIGN; a hit-rate headline would defame them and back-door a
  long bias. Hit-rate is a secondary, interval-bounded figure only.
- HARD MIN_N gate (reuse rr_floor's N_MIN=8): below the floor a bucket prints "insufficient sample"
  and NO number. Global dormancy below `DORMANCY_N` total closed trades.
- Intervals + Bayesian shrinkage, never bare point estimates; Holm multiplicity correction on the
  live family of buckets; an "inconclusive" flag whenever the avg-R CI straddles 0.
- TWO-SIDED / anti-ratchet: always co-surface a "working edge -> take it" line when any bucket has
  positive expectancy; cautions self-silence when the book is flat; caution phrasing is
  "size down / demand confirmation", NEVER "avoid/skip". An advisory must never read as a soft veto.
- FAIL-CLOSED regime join on the news-blind `deterministic_regime`; an un-joinable trade is excluded
  from regime-conditioned buckets (counted in `n_unjoinable`), never coerced to an "unknown" bucket.
"""
from __future__ import annotations

import json
import math
import statistics
from pathlib import Path

from futures_fund.journal import read_all_decisions, realized_total
from futures_fund.vendor.overfit_detector import holm_correction

MIN_N = 8            # decided trades a bucket needs to surface a number (rr_floor precedent)
DORMANCY_N = 60      # total closed trades below which the whole playbook mostly abstains
SHRINK_M = 8.0       # Beta-Binomial prior concentration (pseudo-counts toward the pooled base rate)
Z = 1.959963985      # 95% normal critical value (Wilson + R-CI)

# Two-sided Student-t 0.975 multipliers for small df (1..29); df>=30 -> Z. Keeps small-n R-CIs
# honestly wide without scipy. Deterministic — no RNG anywhere in this module (tests rely on it).
_T975 = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365, 8: 2.306,
         9: 2.262, 10: 2.228, 11: 2.201, 12: 2.179, 13: 2.160, 14: 2.145, 15: 2.131, 16: 2.120,
         17: 2.110, 18: 2.101, 19: 2.093, 20: 2.086, 21: 2.080, 22: 2.074, 23: 2.069, 24: 2.064,
         25: 2.060, 26: 2.056, 27: 2.052, 28: 2.048, 29: 2.045}


def _t975(df: int) -> float:
    return _T975.get(df, Z) if df < 30 else Z


def _finite(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


# ----------------------------------------------------------------------------- R reconstruction
def reconstruct_r(rec: dict) -> tuple[float | None, float | None]:
    """(gross_r, net_r) for a CLOSED trade, or (None, None) if not reconstructible. risk-$ =
    size*|entry-stop|; net_r = realized_pnl/risk; gross_r adds back fees + funding so fee drag is
    visible, not a structural tilt-to-caution. Drops a non-finite / zero-risk / un-closed record."""
    entry, stop, size = rec.get("entry"), rec.get("stop"), rec.get("size")
    # Use the TRUE realized (final close + partial banks) so a scaled-out trade isn't undercounted
    # (cy78 review: realized_total was wired into only _r_multiple; every other consumer read
    # realized_pnl alone and undercounted/misclassified every trimmed trade).
    pnl = realized_total(rec) if rec.get("realized_pnl") is not None else None
    if not (_finite(entry) and _finite(stop) and _finite(size) and _finite(pnl)):
        return None, None
    risk = abs(size) * abs(entry - stop)
    if not (_finite(risk) and risk > 0):
        return None, None
    fees = rec.get("fees") if _finite(rec.get("fees")) else 0.0
    funding = rec.get("funding_paid") if _finite(rec.get("funding_paid")) else 0.0
    net_r = pnl / risk
    gross_r = (pnl + fees + funding) / risk
    return gross_r, net_r


# ----------------------------------------------------------------------------- classification
def classify_setup(rec: dict, regime_label: str | None) -> dict:
    """OUTCOME-BLIND, side-agnostic classification of a trade. Reads ONLY decision-time geometry
    (direction + the recovered regime) — NEVER any outcome field (locked by test). `archetype` is a
    forward axis (always 'unclassified' until setups are stamped at decision time); today the live
    axes are `side` and `regime_alignment`."""
    side = rec.get("direction")
    if side not in ("long", "short"):
        side = "unknown"
    if regime_label == "risk_on":
        alignment = "with" if side == "long" else "counter" if side == "short" else "neutral"
    elif regime_label == "risk_off":
        alignment = "with" if side == "short" else "counter" if side == "long" else "neutral"
    elif regime_label == "mixed":
        alignment = "neutral"
    else:                       # no regime recovered -> not regime-classifiable (excluded later)
        alignment = None
    return {"side": side, "regime_alignment": alignment, "archetype": "unclassified"}


# ----------------------------------------------------------------------------- regime recovery
def load_regime_by_cycle(state_dir) -> dict[int, str]:
    """{cycle_no -> news-blind deterministic_regime} from state/regime_history.jsonl. Read-only;
    tolerant of torn/blank lines; binds the CORRECT key (`deterministic_regime`, fallback `regime`;
    the literal `label` key is null on every row)."""
    out: dict[int, str] = {}
    p = Path(state_dir) / "regime_history.jsonl"
    try:
        text = p.read_text()
    except OSError:
        return out
    for ln in text.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            row = json.loads(ln)
        except ValueError:
            continue
        cyc = row.get("cycle_no")
        lab = row.get("deterministic_regime") or row.get("regime")
        if isinstance(cyc, int) and not isinstance(cyc, bool) and isinstance(lab, str):
            out[cyc] = lab      # last write wins (a reclassified cycle keeps its final label)
    return out


def recover_regime(cycle, regime_by_cycle: dict) -> str | None:
    """FAIL-CLOSED: the regime label for a trade's cycle, or None if there is no row (the trade is
    then EXCLUDED from regime-conditioned buckets and counted in n_unjoinable — never coerced)."""
    if not isinstance(cycle, int) or isinstance(cycle, bool):
        return None
    return regime_by_cycle.get(cycle)


# ----------------------------------------------------------------------------- statistics
def wilson_interval(k: int, n: int) -> tuple[float, float]:
    """95% Wilson score interval for a binomial proportion k/n. (0,1) for n==0."""
    if n <= 0:
        return 0.0, 1.0
    phat = k / n
    z2 = Z * Z
    denom = 1 + z2 / n
    center = (phat + z2 / (2 * n)) / denom
    half = (Z * math.sqrt(phat * (1 - phat) / n + z2 / (4 * n * n))) / denom
    return max(0.0, center - half), min(1.0, center + half)


def mean_ci(values: list[float]) -> tuple[float, float, float]:
    """(mean, lo, hi) — a small-sample two-sided 95% t-interval on the mean (deterministic, no RNG).
    A single point or empty -> a degenerate interval at the mean (n==1) / (0,0,0) (empty)."""
    n = len(values)
    if n == 0:
        return 0.0, 0.0, 0.0
    m = statistics.fmean(values)
    if n == 1:
        return m, m, m
    sd = statistics.stdev(values)
    half = _t975(n - 1) * sd / math.sqrt(n)
    return m, m - half, m + half


def beta_binomial_shrink(k: int, n: int, base_rate: float, m: float = SHRINK_M) -> float:
    """Posterior-mean hit-rate under a Beta(base_rate*m, (1-base_rate)*m) prior — a 2/3 bucket reads
    near the pooled base rate, not 0.67. Pure."""
    a, b = base_rate * m, (1 - base_rate) * m
    return (k + a) / (n + a + b) if (n + a + b) > 0 else base_rate


def _mean_p_value(values: list[float]) -> float:
    """Two-sided p-value for H0: mean R == 0 (normal approx of the t-stat; deterministic). 1.0 when
    n<2 or zero dispersion (no evidence)."""
    n = len(values)
    if n < 2:
        return 1.0
    sd = statistics.stdev(values)
    if sd <= 0:
        return 0.0 if statistics.fmean(values) != 0 else 1.0
    z = abs(statistics.fmean(values)) / (sd / math.sqrt(n))
    return max(0.0, min(1.0, 2 * (1 - statistics.NormalDist().cdf(z))))


# ----------------------------------------------------------------------------- aggregation
def _bucket_stats(rs_net: list[float], rs_gross: list[float], wins: int, base_rate: float) -> dict:
    n = len(rs_net)
    net_m, net_lo, net_hi = mean_ci(rs_net)
    gro_m, _, _ = mean_ci(rs_gross)
    wlo, whi = wilson_interval(wins, n)
    # The sign verdict keys off the UNROUNDED CI bounds (never the rounded mean): a conclusively
    # positive bucket whose mean rounds to 0.000 must still read "pos", never fall to caution.
    straddles = net_lo <= 0.0 <= net_hi
    sign = "zero" if straddles else ("pos" if net_lo > 0.0 else "neg")
    return {
        "n": n, "wins": wins,
        "hit_rate": wins / n if n else 0.0,
        "hit_rate_ci": (round(wlo, 3), round(whi, 3)),
        "hit_rate_shrunk": round(beta_binomial_shrink(wins, n, base_rate), 3),
        "net_r_mean": round(net_m, 3), "net_r_ci": (round(net_lo, 3), round(net_hi, 3)),
        "gross_r_mean": round(gro_m, 3),
        "inconclusive": straddles,   # the R-CI straddles 0 -> no edge sign proven
        "direction_sign": sign,      # pos|neg|zero from the UNROUNDED CI (drives every verdict)
        "p_value": _mean_p_value(rs_net),
    }


def aggregate_playbook(decisions: list[dict], regime_by_cycle: dict, *, min_n: int = MIN_N) -> dict:
    """Mine the journal into expectancy-led per-bucket stats. Buckets: per SIDE (long/short,
    regime-pooled) and per (side, regime_alignment) (surfaced only if the cell clears min_n). Pure;
    fail-safe (skips un-reconstructible / un-classifiable records, counting them). Holm-corrects the
    p-values across the surfaced-bucket family so multiplicity can't mint a false 'notable'."""
    closed = [d for d in decisions if d.get("realized_pnl") is not None]
    by_side: dict[str, dict] = {}
    by_side_regime: dict[tuple, dict] = {}
    n_dropped = n_unjoinable = 0
    regime_cov = {"risk_off": 0, "risk_on": 0, "mixed": 0}
    all_net: list[float] = []
    all_win = 0

    def _slot(store, key):
        return store.setdefault(key, {"net": [], "gross": [], "wins": 0})

    def _push(slot, net_r, gross_r, win):
        slot["net"].append(net_r)
        slot["gross"].append(gross_r)
        slot["wins"] += win

    for d in closed:
        gross_r, net_r = reconstruct_r(d)
        if net_r is None:
            n_dropped += 1
            continue
        regime = recover_regime(d.get("cycle"), regime_by_cycle)
        cls = classify_setup(d, regime)
        side = cls["side"]
        if side not in ("long", "short"):
            n_dropped += 1
            continue
        win = 1 if net_r > 0 else 0
        all_net.append(net_r)
        all_win += win
        _push(_slot(by_side, side), net_r, gross_r, win)
        if regime in regime_cov:
            regime_cov[regime] += 1
        if cls["regime_alignment"] is None:
            n_unjoinable += 1            # no regime row -> excluded from regime-conditioned buckets
        else:
            _push(_slot(by_side_regime, (side, cls["regime_alignment"])), net_r, gross_r, win)

    base_rate = (all_win / len(all_net)) if all_net else 0.5

    side_buckets = {k: _bucket_stats(v["net"], v["gross"], v["wins"], base_rate)
                    for k, v in by_side.items()}
    regime_buckets = {
        f"{k[0]}/{k[1]}-regime": _bucket_stats(v["net"], v["gross"], v["wins"], base_rate)
        for k, v in by_side_regime.items() if len(v["net"]) >= min_n}

    # Holm multiplicity correction across the live family of SURFACED (>=min_n) buckets only.
    surfaced = {k: b for k, b in {**side_buckets, **regime_buckets}.items() if b["n"] >= min_n}
    if surfaced:
        keys = list(surfaced)
        flags = holm_correction([surfaced[k]["p_value"] for k in keys])
        for k, sig in zip(keys, flags, strict=False):
            surfaced[k]["significant"] = bool(sig)
    for b in {**side_buckets, **regime_buckets}.values():
        b.setdefault("significant", False)

    return {
        "side_buckets": side_buckets,
        "regime_buckets": regime_buckets,
        "base_rate": round(base_rate, 3),
        "coverage": {
            "n_closed": len(closed), "n_classified": len(all_net),
            "n_dropped": n_dropped, "n_unjoinable": n_unjoinable,
            "regime_coverage": regime_cov,
        },
        "min_n": min_n,
    }


# ----------------------------------------------------------------------------- advisory string
def _verdict(b: dict, *, book_flat: bool = False) -> str:
    """Symmetric, EXPECTANCY-led label keyed on `direction_sign` (the UNROUNDED CI sign), never the
    rounded mean. A positive-expectancy bucket is NEVER 'caution'; a negative one NEVER 'favorable';
    'inconclusive' when the R-CI spans 0. `book_flat` softens a caution so an idle desk is never
    talked further into cash (the per-line mirror of the summary self-silence)."""
    sign = b.get("direction_sign", "zero")
    if sign == "zero":
        return "inconclusive"
    if sign == "pos":
        return "WORKING — favor it" if b.get("significant") else "leaning favorable"
    return "below par so far (flat — no action implied)" if book_flat \
        else "underperforming — size down / demand extra confirmation"


def _bucket_line(name: str, b: dict, min_n: int, book_flat: bool = False) -> str:
    if b["n"] < min_n:
        return f"  {name}: insufficient sample (n={b['n']}<{min_n}) — keep sampling, no read"
    lo, hi = b["net_r_ci"]
    hlo, hhi = b["hit_rate_ci"]
    return (f"  {name}: {b['n']} trades, net {b['net_r_mean']:+.2f}R [CI {lo:+.2f},{hi:+.2f}] "
            f"(gross {b['gross_r_mean']:+.2f}R), hit {b['hit_rate']:.0%} "
            f"[{hlo:.0%}-{hhi:.0%}; shr {b['hit_rate_shrunk']:.0%}] "
            f"-> {_verdict(b, book_flat=book_flat)}")


def format_playbook_advisory(agg: dict, *, book_flat: bool = False,
                             total_closed: int | None = None, dormancy_n: int = DORMANCY_N) -> str:
    """The human-readable advisory injected into the RM. Expectancy-led, TWO-SIDED (always
    co-surfaces a working-edge line), cautions self-silence when flat, cold-start tiers."""
    cov = agg.get("coverage", {})
    n_closed = total_closed if total_closed is not None else cov.get("n_closed", 0)
    min_n = agg.get("min_n", MIN_N)
    side = agg.get("side_buckets", {})
    regime = agg.get("regime_buckets", {})

    head = "PLAYBOOK (your realized track record — advisory; your own past choices shaped it):"

    if n_closed == 0:
        return f"{head}\n  no record yet — neutral; hunt setups on their own merits."
    established = {k: b for k, b in {**side, **regime}.items() if b["n"] >= min_n}
    if n_closed < dormancy_n and not established:
        return (f"{head}\n  cold-starting ({n_closed}/{dormancy_n} closed trades, no bucket at n>="
                f"{min_n} yet) — the playbook ABSTAINS; trade your thesis. It sharpens later.")

    lines = [head]
    # side buckets first (the regime-pooled, honest-today view), then any qualified regime cells
    for nm in ("long", "short"):
        if nm in side:
            lines.append(_bucket_line(f"{nm} book", side[nm], min_n, book_flat=book_flat))
    for nm, b in regime.items():
        lines.append(_bucket_line(nm, b, min_n, book_flat=book_flat))

    # TWO-SIDED contract (sign keyed off the UNROUNDED CI): surface every WORKING edge with equal
    # prominence; never only-caution.
    working = [nm for nm, b in {**side, **regime}.items()
               if b["n"] >= min_n and b.get("direction_sign") == "pos"]
    if working:
        lines.append(f"  EDGE WORKING: {', '.join(working)} carries positive expectancy — do NOT "
                     "stand flat on a clean, gate-clearing setup here; taking it is NOT forcing.")
    # cautions self-silence when the book is flat (an idle desk must not be talked into more cash)
    if not book_flat:
        weak = [nm for nm, b in {**side, **regime}.items()
                if b["n"] >= min_n and b.get("direction_sign") == "neg"]
        if weak:
            lines.append(f"  SIZE-DOWN (not avoid): {', '.join(weak)} is underperforming — demand "
                         "extra confirmation / smaller size, but keep taking qualifying setups.")
    rc = cov.get("regime_coverage", {})
    lines.append(f"  coverage: {cov.get('n_classified', 0)} classified of {n_closed} closed "
                 f"(dropped {cov.get('n_dropped', 0)}, no-regime {cov.get('n_unjoinable', 0)}); "
                 f"per-regime risk_off={rc.get('risk_off', 0)} risk_on={rc.get('risk_on', 0)} "
                 f"mixed={rc.get('mixed', 0)} — regime cells below n={min_n} POOLED, not shown.")
    return "\n".join(lines)


# ----------------------------------------------------------------------------- top-level
def playbook_advisory(memory_dir, state_dir, *, book_flat: bool = False) -> str:
    """Read-only end to end: journal + regime_history -> the RM advisory string. FAIL-SAFE — any
    error yields a benign abstaining line, never raises into the cycle."""
    try:
        decisions = read_all_decisions(memory_dir)
        regime_by_cycle = load_regime_by_cycle(state_dir)
        agg = aggregate_playbook(decisions, regime_by_cycle)
        n_closed = agg["coverage"]["n_closed"]
        return format_playbook_advisory(agg, book_flat=book_flat, total_closed=n_closed)
    except Exception:  # noqa: BLE001 — advisory must never break the cycle (orchestration pattern)
        return ("PLAYBOOK: unavailable this cycle (read error) — abstaining; trade your thesis.")

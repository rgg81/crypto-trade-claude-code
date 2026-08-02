from __future__ import annotations

import math
from datetime import datetime

from futures_fund.baseline import _atr, adx, ema_slope, rsi, simple_regime, swing_levels

_TF_SECONDS = {"15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}
OI_REACTIVE_LOOKBACK = 4   # completed 4h bars back (~16h) for the trigger OI-gate's reactive window


# Binance's funding formula is F = premium + clamp(interest - premium, ±0.05%), with a default
# interest component of 0.01% per 8h (pro-rated to the symbol's own interval). A perp whose basis
# sits anywhere inside that clamp therefore prints EXACTLY the interest rate. Annualized, that
# zero-information point is interval-INVARIANT: 0.01% x 3 x 365 = 10.95%/yr.
FUNDING_INTEREST_PER_8H = 0.0001
FUNDING_BASELINE_ANNUAL_PCT = FUNDING_INTEREST_PER_8H * 3.0 * 365.0 * 100.0  # 10.95


def funding_premium(annualized_pct: float | None, *,
                    baseline_pct: float = FUNDING_BASELINE_ANNUAL_PCT,
                    tol_pct: float = 0.05) -> tuple[str, float | None]:
    """Classify funding RELATIVE TO the baseline — the read `funding_payer` cannot give.

    `funding_payer == "longs"` is true for ANY rate > 0, so the desk spent cycles reading the
    BASELINE ITSELF as "trapped longs bleeding carry" (cy309: HYPE printed +10.95%/yr = exactly
    baseline and was called 'flush fuel restored'; BNB's +7.06% was called 'the cleanest carry on
    the board' while actually sitting at a 64.5%-of-baseline DISCOUNT). Only the distance from
    baseline carries information:
      - "premium"  (> baseline): longs really are paying up — the only genuine
        flush-short carry leg.
      - "discount" (< baseline): the perp trades below spot = net short pressure, even when
        `funding_payer` still says "longs".
      - "at_par"   (≈ baseline): INDETERMINATE, not "zero premium" — the clamp pins F at the
        interest rate across the whole band where the basis is within ~5bp of it, so an at-par
        print says nothing about crowding either way. Never score it as carry fuel.
    Returns (state, ratio_to_baseline); ("unknown", None) on missing/degraded input."""
    try:
        if annualized_pct is None or not math.isfinite(float(annualized_pct)) or not baseline_pct:
            return ("unknown", None)
        annual = float(annualized_pct)
        ratio = annual / baseline_pct
        if abs(annual - baseline_pct) <= tol_pct:
            return ("at_par", ratio)
        return ("premium" if annual > baseline_pct else "discount", ratio)
    except Exception:  # noqa: BLE001 — never break the brief over funding housekeeping
        return ("unknown", None)


def funding_direction(rate: float, interval_hours: float) -> tuple[str, float]:
    """Resolve a raw funding rate into an UNAMBIGUOUS (payer, annualized_pct) pair so agents never
    have to interpret the sign — the cy78 trap, where two analysts inverted it and called a TRX
    short (negative funding) a carry tailwind when a short there PAYS ~106%/yr.

    Convention (standard perp): rate>0 => longs pay shorts ('longs'); rate<0 => shorts pay longs
    ('shorts'); rate==0 => 'none'. So the named side PAYS (a carry DRAG) and the other RECEIVES.
    annualized_pct = rate * (24/interval_hours) * 365 * 100, SIGNED (keeps the raw direction)."""
    try:
        if not math.isfinite(rate) or not interval_hours or not math.isfinite(interval_hours):
            return ("none", 0.0)
        annual = rate * (24.0 / interval_hours) * 365.0 * 100.0
        payer = "longs" if rate > 0 else ("shorts" if rate < 0 else "none")
        return (payer, annual)
    except Exception:  # noqa: BLE001 — never break the brief over funding housekeeping
        return ("none", 0.0)


def last_completed_frame(df, now: datetime | None, timeframe: str = "4h"):
    """Drop the still-FORMING last candle so 'last close', momentum, and trigger evaluation read the
    last COMPLETED bar — not a transient intra-candle print. The OHLCV feed returns the in-progress
    candle (open-ts == the current window) as the last row; if `now` falls inside that window, that
    row is dropped. An already-closed last candle (or no `now`) is left untouched, and a single-row
    frame is never emptied. ctx.prices keeps the live last row for MARK-TO-MARKET equity; trigger
    AND exit evaluation both call this so they read the same completed bar (cy77 fix)."""
    if df is None or not len(df) or now is None or len(df) < 2:
        return df
    try:
        secs = _TF_SECONDS.get(timeframe, 14400)
        ts = df["timestamp"].iloc[-1]
        ts = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
        if ts.tzinfo is None:
            from datetime import UTC
            ts = ts.replace(tzinfo=UTC)
        if (now - ts).total_seconds() < secs:   # last row's window has not closed yet -> forming
            return df.iloc[:-1]
    except Exception:  # noqa: BLE001 — never break the cycle over bar housekeeping
        pass
    return df


def oi_change_for(exchange, symbol: str, timeframe: str = "4h", now: datetime | None = None,
                  lookback: int = OI_REACTIVE_LOOKBACK) -> float | None:
    """REACTIVE, completed-bar-aligned open-interest change for the trigger OI-confirmation gate:
    (last completed OI bar / the bar ~`lookback` intervals prior) - 1.0. DELIBERATELY a shorter,
    more reactive window than `_derivatives`' 48h analyst trend — it confirms fresh fuel arriving ON
    a break (trapped longs flushing / shorts trapping right now), not a 2-day positioning trend.
    Drops the still-FORMING OI row (via last_completed_frame) so it reads the SAME completed-bar
    frame the trigger fires on — never a tick-mutating forming value (the bug last_completed_frame
    exists to kill). Pass `now` (the gate's fire-time instant) for completed-bar alignment; with
    now=None NO row is dropped, so a caller wanting alignment MUST supply it. Returns None on any
    feed error, NaN, zero base, or a too-short series; the
    pending-orders gate treats None as 'unconfirmed' and HOLDS the trigger (fail-safe: a missing
    reading can NEVER cause a spurious fire). NOTE: rising AGGREGATE OI is a necessary-but-not-
    sufficient proxy for new positioning in the break direction (it cannot distinguish new-shorts
    from new-longs) — a fuel filter, not a direction oracle."""
    try:
        oi = exchange.open_interest_history(symbol, period=timeframe, limit=lookback + 3)
        oi = last_completed_frame(oi, now, timeframe)
        s = oi["oi_value"]
        if len(s) < 2:
            return None
        last = float(s.iloc[-1])
        base = float(s.iloc[max(0, len(s) - 1 - lookback)])
        if not base or math.isnan(base) or math.isnan(last):
            return None
        return last / base - 1.0
    except Exception:  # noqa: BLE001 — any feed/parse error -> None -> gate HOLDS (fail-safe)
        return None


def flag_duplicate_positioning(briefs: list[dict]) -> list[dict]:
    """DATA-INTEGRITY guard (cy50): the Binance globalLongShortAccountRatio feed can ALIAS one
    symbol's positioning onto another — observed DOGE returning ETH's long_short_ratio 2.3456 AND
    long_account 0.7011 byte-identical (reproducible even with the correct raw id). Identical
    positioning across DISTINCT symbols is a feed-alias bug, not market reality, and we cannot tell
    which symbol the value really belongs to. So when >=2 briefs share the SAME non-null
    (long_short_ratio, long_account) pair, NULL those two fields for EVERY member of the group and
    stamp `positioning_anomaly='duplicate_ls_feed'` so the analysts down-weight (they fall back to
    price/OI). Fail-safe: nulling positioning only DEGRADES a signal, never fabricates one;
    requiring BOTH fields to match exactly makes a false positive (two distinct symbols
    legitimately identical to full precision) vanishingly unlikely. Mutates+returns the briefs."""
    from collections import defaultdict
    groups: dict = defaultdict(list)
    for b in briefs:
        lsr = b.get("long_short_ratio")
        la = b.get("long_account")
        if lsr is None or la is None:
            continue
        groups[(lsr, la)].append(b)
    for members in groups.values():
        # DISTINCT symbols only — the same symbol appearing twice (e.g. a regime-panel duplicate)
        # is not an alias; an alias is two different ids carrying the same positioning row.
        syms = {m.get("exchange_id") or m.get("symbol") for m in members}
        if len(syms) > 1:
            for m in members:
                m["long_short_ratio"] = None
                m["long_account"] = None
                m["positioning_anomaly"] = "duplicate_ls_feed"
    return briefs


def _derivatives(exchange, symbol: str, timeframe: str, now: datetime | None = None) -> dict:
    """OI trend + long/short positioning; all-None if the feed is unavailable (graceful). Reads the
    LAST COMPLETED bar (drops the still-forming one via last_completed_frame) so positioning matches
    the brief's completed-bar price — the same forming-candle discipline OHLCV and the OI trigger
    gate apply. This also sidesteps the simulated globalLongShortAccountRatio feed-alias, which is
    byte-identical only on the FORMING bar (cy50: DOGE==ETH on the in-progress candle) while the
    CLOSED bar is clean per symbol — so reading the closed bar avoids the alias at the source (the
    flag_duplicate_positioning de-dupe stays a fail-safe backstop). Pass `now` for the drop."""
    out = {"oi_value": None, "oi_change": None, "oi_amount": None, "oi_change_coin": None,
           "long_short_ratio": None, "long_account": None}
    try:
        oi = last_completed_frame(
            exchange.open_interest_history(symbol, period=timeframe, limit=12), now, timeframe)
        if len(oi) > 1:
            out["oi_value"] = float(oi["oi_value"].iloc[-1])
            base = oi["oi_value"].iloc[0]
            out["oi_change"] = float(oi["oi_value"].iloc[-1] / base - 1.0) if base else 0.0
            # COIN/CONTRACT count alongside the USD notional (cy311). `oi_value` is contracts x
            # PRICE, so its % change conflates positioning with price — and the error is
            # DIRECTIONALLY BIASED: in a falling market USD-OI drops even while contracts build,
            # and in a rising one it inflates a build that is mostly price. That systematically
            # strips the fuel leg off with-regime SHORT theses while over-crediting LONGs, which is
            # a long/short-symmetry defect (Rule 5), not a rounding quibble — it is what
            # manufactured the desk's 13-cycle "no with-regime short has rising OI" reading (cy311:
            # HYPE -2.96% USD vs +1.08% contracts; UNI +12.53% vs +5.82%; ZEC +0.83% vs -0.50%,
            # a sign INVERSION). Score every fuel leg on the coin count; report both when they
            # disagree in sign.
            amt_base = oi["oi_amount"].iloc[0]
            out["oi_amount"] = float(oi["oi_amount"].iloc[-1])
            out["oi_change_coin"] = (float(oi["oi_amount"].iloc[-1] / amt_base - 1.0)
                                     if amt_base else 0.0)
    except Exception:
        pass
    try:
        lsr = last_completed_frame(
            exchange.long_short_ratio(symbol, period=timeframe, limit=6), now, timeframe)
        if len(lsr):
            out["long_short_ratio"] = float(lsr["long_short_ratio"].iloc[-1])
            out["long_account"] = float(lsr["long_account"].iloc[-1])
    except Exception:
        pass
    return out


_BAR_RANGE_LOOKBACK = 12


def bar_range_stats(df, lookback: int = _BAR_RANGE_LOOKBACK) -> dict:
    """Median / mean / max HIGH-LOW range over the last `lookback` COMPLETED bars.

    Feeds lesson 7d65f48b: a `limit_entry` whose (trigger - stop) gap is smaller than the range a
    single bar routinely spans gets knife-guard CONSUMED (both legs tagged in one bar, no fill ever
    booked) -- the cy317 UNIUSDT failure. ATR cannot substitute: it is a smoothed average and
    understates the tail, so a gap that looks safe in ATR terms can still sit inside half the bars'
    actual swing. `bar_range_max` is reported alongside the median precisely so that tail is visible
    (cy319 EULUSDT: median 0.727 ATR, max 3.127 ATR -- a 4.3x spread).

    FAIL-SAFE (Rule 4): a missing/short/malformed frame degrades to None rather than raising --
    a feed gap must never break the cycle.
    """
    keys = ("bar_range_median", "bar_range_mean", "bar_range_max")
    try:
        rows = df[["high", "low"]].tail(int(lookback))
        rng = (rows["high"] - rows["low"]).dropna()
        if rng.empty:
            return dict.fromkeys(keys)
        return {"bar_range_median": float(rng.median()),
                "bar_range_mean": float(rng.mean()),
                "bar_range_max": float(rng.max())}
    except Exception:  # noqa: BLE001 -- brief assembly must never raise on a feed gap
        return dict.fromkeys(keys)


def build_symbol_brief(exchange, symbol: str, timeframe: str = "4h",
                       now: datetime | None = None) -> dict:
    """Compact, JSON-serializable per-symbol data bundle the orchestrator injects into the
    analyst subagents' prompts. Pure-ish: reads only from the injected exchange. `now` (when given)
    drops the still-forming last candle so last_close/momentum/regime read the last COMPLETED bar."""
    df = last_completed_frame(exchange.ohlcv(symbol, timeframe), now, timeframe)
    funding = exchange.funding(symbol)
    close = df["close"]
    last = float(close.iloc[-1])
    regime = simple_regime(df)
    mom_20 = float(close.iloc[-1] / close.iloc[-21] - 1.0) if len(close) > 21 else 0.0
    # COMPUTED technical indicators (the Technical analyst reads THESE — never invents them): RSI +
    # ADX(+DI/-DI) for momentum/trend-strength, EMA-20/50 slopes, swing hi/lo for real S/R.
    adx_val, plus_di, minus_di = adx(df)
    swing_high, swing_low = swing_levels(df)
    ranges = bar_range_stats(df)
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "last_close": last,
        "regime": regime.quadrant,
        "trend_direction": regime.trend_direction,
        "atr": float(_atr(df)),
        "momentum_20": mom_20,
        "rsi": rsi(df),
        "adx": adx_val,
        "plus_di": plus_di,
        "minus_di": minus_di,
        "ema20_slope": ema_slope(df, 20),
        "ema50_slope": ema_slope(df, 50),
        "swing_high": swing_high,
        "swing_low": swing_low,
        "dist_to_swing_high_pct": round((swing_high - last) / last, 4) if last else None,
        "dist_to_swing_low_pct": round((last - swing_low) / last, 4) if last else None,
        # REALIZED completed-bar ranges (cy319). Lesson 7d65f48b requires sizing a limit_entry's
        # trigger-to-stop gap against the range a SINGLE bar actually spans — the ~0.6-ATR noise
        # band answers "can this stop survive normal wobble", not "can one bar tag BOTH legs".
        # ATR alone cannot answer that (it is a smoothed average, so it understates the fat tail:
        # at cy319 EULUSDT's median bar range was 0.727 ATR but its max was 3.127 ATR). Without
        # these numbers in the brief the RM could not evaluate its own lesson and hedged by
        # sizing down — a lesson whose inputs are absent is observationally identical to one that
        # does not exist (the d6da6f70 silent-off-switch pattern, applied to lesson INPUTS).
        **ranges,
        "funding_rate": float(funding.current_rate),
        "funding_interval_hours": float(funding.interval_hours),
        # Explicit, sign-proof funding direction (cy78 fix): funding_payer is the side that PAYS
        # (a carry DRAG); the other side RECEIVES. Read THIS — never infer carry from funding_rate.
        "funding_payer": funding_direction(float(funding.current_rate),
                                           float(funding.interval_hours))[0],
        "funding_annualized_pct": funding_direction(float(funding.current_rate),
                                                    float(funding.interval_hours))[1],
        # Baseline-RELATIVE read (cy309): `funding_payer` is a pure sign test, so it labels the
        # zero-information baseline as "longs pay". Agents must score carry off these instead.
        "funding_baseline_pct": FUNDING_BASELINE_ANNUAL_PCT,
        "funding_premium": funding_premium(funding_direction(
            float(funding.current_rate), float(funding.interval_hours))[1])[0],
        "funding_vs_baseline": funding_premium(funding_direction(
            float(funding.current_rate), float(funding.interval_hours))[1])[1],
        "mark_price": float(funding.mark_price),
        **_derivatives(exchange, symbol, timeframe, now=now),
    }

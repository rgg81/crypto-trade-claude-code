from __future__ import annotations

import math
from datetime import datetime

from futures_fund.baseline import _atr, adx, ema_slope, rsi, simple_regime, swing_levels

_TF_SECONDS = {"15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}
OI_REACTIVE_LOOKBACK = 4   # completed 4h bars back (~16h) for the trigger OI-gate's reactive window


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
    out = {"oi_value": None, "oi_change": None, "long_short_ratio": None, "long_account": None}
    try:
        oi = last_completed_frame(
            exchange.open_interest_history(symbol, period=timeframe, limit=12), now, timeframe)
        if len(oi) > 1:
            out["oi_value"] = float(oi["oi_value"].iloc[-1])
            base = oi["oi_value"].iloc[0]
            out["oi_change"] = float(oi["oi_value"].iloc[-1] / base - 1.0) if base else 0.0
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
        "funding_rate": float(funding.current_rate),
        "funding_interval_hours": float(funding.interval_hours),
        "mark_price": float(funding.mark_price),
        **_derivatives(exchange, symbol, timeframe, now=now),
    }

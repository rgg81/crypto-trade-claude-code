from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
from pydantic import BaseModel

from futures_fund.models import MmrBracket, SymbolSpec


class FundingInfo(BaseModel):
    symbol: str
    current_rate: float       # ccxt fundingRate = Binance lastFundingRate (current, not predicted)
    next_funding_ts: datetime
    interval_hours: float
    mark_price: float
    index_price: float


def parse_symbol_spec(market: dict, tiers: list[dict]) -> SymbolSpec:
    """ccxt market dict + leverage tiers -> SymbolSpec. precisionMode is TICK_SIZE so
    precision.price/amount ARE the tick/step sizes."""
    brackets = [
        MmrBracket(
            notional_floor=float(t["minNotional"]),
            notional_cap=float(t["maxNotional"]),
            mmr=float(t["maintenanceMarginRate"]),
            maint_amount=float(t["info"]["cum"]),
            max_leverage=float(t["maxLeverage"]),
        )
        for t in tiers
    ]
    return SymbolSpec(
        symbol=market["id"],
        tick_size=float(market["precision"]["price"]),
        step_size=float(market["precision"]["amount"]),
        min_notional=float(market["limits"]["cost"]["min"]),
        mmr_brackets=brackets,
    )


def parse_ohlcv(rows: list[list]) -> pd.DataFrame:
    """ccxt OHLCV rows [[ts_ms,o,h,l,c,v], ...] -> sorted UTC-timestamped DataFrame."""
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return (
        df[["timestamp", "open", "high", "low", "close", "volume"]]
        .sort_values("timestamp")
        .reset_index(drop=True)
    )


def parse_funding(fr: dict, interval: dict | None = None) -> FundingInfo:
    interval_hours = 8.0
    if interval and (interval.get("info") or {}).get("fundingIntervalHours") is not None:
        interval_hours = float(interval["info"]["fundingIntervalHours"])
    return FundingInfo(
        symbol=fr["symbol"],
        current_rate=float(fr["fundingRate"]),
        next_funding_ts=datetime.fromtimestamp(fr["fundingTimestamp"] / 1000, tz=timezone.utc),  # noqa: UP017
        interval_hours=interval_hours,
        mark_price=float(fr["markPrice"]),
        index_price=float(fr["indexPrice"]),
    )


def parse_open_interest_history(rows: list[dict]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=["timestamp", "oi_amount", "oi_value"])
    recs = [
        {
            "timestamp": pd.to_datetime(int(r["timestamp"]), unit="ms", utc=True),
            "oi_amount": float(r["openInterestAmount"]),
            "oi_value": (
                float(r["openInterestValue"])
                if r.get("openInterestValue") is not None
                else float("nan")
            ),
        }
        for r in rows
    ]
    return pd.DataFrame(recs).sort_values("timestamp").reset_index(drop=True)


def parse_long_short_ratio(raw_rows: list[dict]) -> pd.DataFrame:
    if not raw_rows:
        return pd.DataFrame(
            columns=["timestamp", "long_short_ratio", "long_account", "short_account"]
        )
    recs = [
        {
            "timestamp": pd.to_datetime(int(r["timestamp"]), unit="ms", utc=True),
            "long_short_ratio": float(r["longShortRatio"]),
            "long_account": float(r["longAccount"]),
            "short_account": float(r["shortAccount"]),
        }
        for r in raw_rows
    ]
    return pd.DataFrame(recs).sort_values("timestamp").reset_index(drop=True)

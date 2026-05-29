from futures_fund.market_data import (
    FundingInfo,
    parse_funding,
    parse_long_short_ratio,
    parse_ohlcv,
    parse_open_interest_history,
    parse_symbol_spec,
)

MARKET = {
    "id": "BTCUSDT",
    "symbol": "BTC/USDT:USDT",
    "precision": {"price": 0.1, "amount": 0.001},
    "limits": {"amount": {"min": 0.001, "max": 1000.0}, "cost": {"min": 100.0}},
    "contractSize": 1.0,
    "info": {"filters": []},
}
TIERS = [
    {"tier": 1, "minNotional": 0, "maxNotional": 50000, "maintenanceMarginRate": 0.004,
     "maxLeverage": 125, "info": {"cum": "0"}},
    {"tier": 2, "minNotional": 50000, "maxNotional": 250000, "maintenanceMarginRate": 0.005,
     "maxLeverage": 100, "info": {"cum": "50"}},
]


def test_parse_symbol_spec_maps_precision_and_brackets():
    spec = parse_symbol_spec(MARKET, TIERS)
    assert spec.symbol == "BTCUSDT"
    assert spec.tick_size == 0.1
    assert spec.step_size == 0.001
    assert spec.min_notional == 100.0
    assert len(spec.mmr_brackets) == 2
    b1 = spec.mmr_brackets[1]
    assert (b1.notional_floor, b1.notional_cap, b1.mmr, b1.maint_amount, b1.max_leverage) == \
        (50000.0, 250000.0, 0.005, 50.0, 100.0)


def test_parse_ohlcv_to_sorted_utc_dataframe():
    rows = [[1780000000000, 100.0, 105.0, 99.0, 104.0, 12.0],
            [1779996400000, 98.0, 101.0, 97.0, 100.0, 8.0]]  # out of order on purpose
    df = parse_ohlcv(rows)
    assert list(df.columns) == ["timestamp", "open", "high", "low", "close", "volume"]
    assert len(df) == 2
    assert df["timestamp"].is_monotonic_increasing
    assert str(df["timestamp"].dt.tz) == "UTC"
    assert df.iloc[-1]["close"] == 104.0


def test_parse_funding_uses_interval_or_defaults_8h():
    fr = {"symbol": "BTC/USDT:USDT", "fundingRate": 0.0001, "fundingTimestamp": 1780041600000,
          "markPrice": 73676.1, "indexPrice": 73702.25}
    fi = parse_funding(fr, {"interval": "4h", "info": {"fundingIntervalHours": 4}})
    assert isinstance(fi, FundingInfo)
    assert fi.current_rate == 0.0001
    assert fi.interval_hours == 4.0
    assert fi.mark_price == 73676.1
    assert str(fi.next_funding_ts.tzinfo) == "UTC"
    # absent interval -> default 8h
    assert parse_funding(fr, None).interval_hours == 8.0


def test_parse_open_interest_history():
    rows = [{"timestamp": 1780000000000, "openInterestAmount": 1234.5, "openInterestValue": 9.0e7},
            {"timestamp": 1779996400000, "openInterestAmount": 1200.0, "openInterestValue": 8.7e7}]
    df = parse_open_interest_history(rows)
    assert list(df.columns) == ["timestamp", "oi_amount", "oi_value"]
    assert df["timestamp"].is_monotonic_increasing
    assert df.iloc[-1]["oi_amount"] == 1234.5


def test_parse_long_short_ratio_casts_strings():
    raw = [{"symbol": "BTCUSDT", "longShortRatio": "1.5", "longAccount": "0.6",
            "shortAccount": "0.4", "timestamp": "1780000000000"}]
    df = parse_long_short_ratio(raw)
    assert df.iloc[0]["long_short_ratio"] == 1.5
    assert df.iloc[0]["long_account"] == 0.6


def test_parse_open_interest_empty_returns_empty_df():
    df = parse_open_interest_history([])
    assert df.empty


def test_parse_symbol_spec_prefers_raw_filters_over_precision():
    # precision given as decimal-PLACES (8, 3) which would be wrong if used as tick/step;
    # the raw filters must win and yield correct sizes.
    market = {
        **MARKET,
        "precision": {"price": 8, "amount": 3},
        "info": {"filters": [
            {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
            {"filterType": "LOT_SIZE", "stepSize": "0.001"},
            {"filterType": "MIN_NOTIONAL", "notional": "100"},
        ]},
    }
    spec = parse_symbol_spec(market, TIERS)
    assert spec.tick_size == 0.1
    assert spec.step_size == 0.001
    assert spec.min_notional == 100.0


def test_parse_long_short_ratio_skips_malformed_rows():
    raw = [
        {"symbol": "BTCUSDT", "longShortRatio": "1.5", "longAccount": "0.6",
         "shortAccount": "0.4", "timestamp": "1780000000000"},
        {"symbol": "BTCUSDT"},  # malformed: missing fields -> skipped, not fatal
    ]
    df = parse_long_short_ratio(raw)
    assert len(df) == 1
    assert df.iloc[0]["long_short_ratio"] == 1.5

"""Candidate price cards (cy78 backlog): a deterministic, compact extract of each screened symbol's
REAL levels (price, ATR, swings, DMI/RSI, funding direction) the orchestrator injects into the
debate/RM/Trader prompts so agents anchor geometry on ground truth — never a hallucinated price (the
cy78 RM priced XMR at ~$185 when the real price was ~$387, a 2x error)."""
from futures_fund.price_card import price_card, price_cards


def _brief(symbol="XMR/USDT:USDT"):
    return {"symbol": symbol, "last_close": 387.21, "mark_price": 380.50, "atr": 20.84,
            "swing_high": 428.97, "swing_low": 304.49, "dist_to_swing_high_pct": 0.1078,
            "dist_to_swing_low_pct": 0.2136, "adx": 38.05, "plus_di": 33.98, "minus_di": 9.03,
            "rsi": 72.25, "ema20_slope": 0.0108, "ema50_slope": 0.0048, "regime": "high_vol_trend",
            "trend_direction": "up", "funding_rate": 0.0001, "funding_payer": "longs",
            "funding_annualized_pct": 10.95, "long_short_ratio": 1.76, "oi_change": 0.227}


def test_price_card_carries_the_real_decision_levels():
    c = price_card(_brief())
    assert c["symbol"] == "XMR/USDT:USDT"
    assert c["last_close"] == 387.21 and c["mark_price"] == 380.50   # the REAL price, not a guess
    assert c["atr"] == 20.84
    assert c["swing_high"] == 428.97 and c["swing_low"] == 304.49
    assert c["adx"] == 38.05 and c["plus_di"] == 33.98 and c["minus_di"] == 9.03
    assert c["funding_rate"] == 0.0001
    assert c["funding_payer"] == "longs" and c["funding_annualized_pct"] == 10.95


def test_price_card_carries_signed_negative_funding_the_cy78_trap():
    # the funding SIGN-trap field: a short-pays symbol (negative rate) surfaces with payer + annual
    c = price_card({"symbol": "TRX/USDT:USDT", "last_close": 0.31, "funding_rate": -0.000968,
                    "funding_payer": "shorts", "funding_annualized_pct": -105.99})
    assert c["funding_rate"] == -0.000968
    assert c["funding_payer"] == "shorts" and c["funding_annualized_pct"] < 0


def test_price_card_is_tolerant_of_missing_fields():
    c = price_card({"symbol": "BTCUSDT", "last_close": 64000.0})
    assert c["symbol"] == "BTCUSDT" and c["last_close"] == 64000.0
    assert c["atr"] is None and c["swing_high"] is None        # absent -> None, never raises


def test_price_cards_filters_to_requested_symbols():
    ctx = {"briefs": [_brief("XMR/USDT:USDT"), _brief("BTC/USDT:USDT"), _brief("ETH/USDT:USDT")]}
    cards = price_cards(ctx, symbols=["XMR/USDT:USDT", "ETH/USDT:USDT"])
    assert {c["symbol"] for c in cards} == {"XMR/USDT:USDT", "ETH/USDT:USDT"}


def test_price_cards_accepts_a_bare_briefs_list_and_returns_all_when_no_filter():
    briefs = [_brief("XMR/USDT:USDT"), _brief("BTC/USDT:USDT")]
    assert len(price_cards(briefs)) == 2
    assert len(price_cards({"briefs": briefs})) == 2


def test_price_cards_filter_matches_raw_or_unified_symbol_shape():
    # briefs are keyed by the UNIFIED id; the screen emits the RAW id. The filter must match either,
    # so price cards work straight off screen.json (the cy78 CLI-returned-nothing bug).
    ctx = {"briefs": [_brief("XMR/USDT:USDT"), _brief("BTC/USDT:USDT")]}
    assert {c["symbol"] for c in price_cards(ctx, symbols=["XMRUSDT"])} == {"XMR/USDT:USDT"}
    assert {c["symbol"] for c in price_cards(ctx, symbols=["XMR/USDT:USDT"])} == {"XMR/USDT:USDT"}


def test_price_cards_empty_or_malformed_is_empty_not_raise():
    assert price_cards(None) == []
    assert price_cards({}) == []
    assert price_cards({"briefs": "garbage"}) == []

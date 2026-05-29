from futures_fund.contracts import AnalystReport
from futures_fund.screen import screen_reports, symbol_conviction


def _r(symbol, stance, conf, agent="technical"):
    return AnalystReport(agent=agent, symbol=symbol, stance=stance, confidence=conf)


def test_symbol_conviction_nets_bullish_minus_bearish_weighted_by_agreement():
    reports = [_r("BTCUSDT", "bullish", 0.8, "technical"),
               _r("BTCUSDT", "bullish", 0.6, "derivatives"),
               _r("BTCUSDT", "neutral", 0.5, "news")]
    # net stance = +0.8 +0.6 +0 = 1.4; agreement = 2 bullish -> conviction = |1.4| * 2
    assert symbol_conviction(reports) == 1.4 * 2


def test_screen_keeps_top_n_by_conviction():
    reports = [
        _r("BTCUSDT", "bullish", 0.9, "technical"), _r("BTCUSDT", "bullish", 0.9, "derivatives"),
        _r("ETHUSDT", "bullish", 0.5, "technical"),
        _r("SOLUSDT", "bearish", 0.8, "technical"), _r("SOLUSDT", "bearish", 0.7, "derivatives"),
    ]
    top = screen_reports(reports, top_n=2)
    assert set(top) == {"BTCUSDT", "SOLUSDT"}     # ETH (single weak signal) screened out
    assert top[0] == "BTCUSDT"                     # strongest first


def test_screen_handles_fewer_than_n():
    top = screen_reports([_r("BTCUSDT", "bullish", 0.5)], top_n=5)
    assert top == ["BTCUSDT"]


def test_screen_drops_pure_neutral_symbols():
    top = screen_reports([_r("BTCUSDT", "neutral", 0.9), _r("BTCUSDT", "neutral", 0.8)], top_n=5)
    assert top == []     # zero net conviction -> not worth debating

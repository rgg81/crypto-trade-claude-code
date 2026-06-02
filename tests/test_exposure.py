"""Soft dollar-neutral exposure layer (market-neutral mandate). book_exposure measures gross
long $ vs gross short $ and the net tilt; exposure_warning is the SYMMETRIC nag that pressures a
materially tilted book back toward neutral — a net-long book is nagged exactly as hard as a
net-short one. No hard veto; this is visibility + pressure only."""
from datetime import UTC, datetime

from futures_fund.portfolio import book_exposure, exposure_warning
from futures_fund.state import Position

_TS = datetime(2026, 3, 1, tzinfo=UTC)


def _pos(symbol, direction, qty, entry, stop=None):
    # default stop is LOSS-SIDE per direction (long: below entry; short: above entry) = risk-bearing
    if stop is None:
        stop = entry * 0.95 if direction == "long" else entry * 1.05
    return Position(symbol=symbol, direction=direction, qty=qty, entry=entry, stop=stop,
                    take_profits=[entry * 1.1], leverage=2.0, margin=100.0, liq_price=entry * 0.5,
                    opened_cycle=1, opened_ts=_TS)


def test_flat_book_is_neutral():
    e = book_exposure([], {}, equity=10_000.0)
    assert e["gross_long"] == 0 and e["gross_short"] == 0 and e["net"] == 0
    assert e["tilt"] == 0.0 and exposure_warning(e) is None


def test_single_long_is_fully_tilted():
    e = book_exposure([_pos("BTCUSDT", "long", 0.1, 1000.0)], {"BTCUSDT": 1000.0}, equity=10_000.0)
    assert e["gross_long"] == 100.0 and e["gross_short"] == 0.0
    assert e["net"] == 100.0 and e["tilt"] == 1.0 and e["long_share"] == 1.0
    w = exposure_warning(e)
    assert w is not None and "LONG" in w and "SHORT" in w  # nags to add a short


def test_dollar_neutral_book_no_warning():
    # equal gross long and short notional -> net 0, perfectly balanced
    pos = [_pos("BTCUSDT", "long", 0.1, 1000.0), _pos("ETHUSDT", "short", 1.0, 100.0)]
    e = book_exposure(pos, {"BTCUSDT": 1000.0, "ETHUSDT": 100.0}, equity=10_000.0)
    assert e["gross_long"] == 100.0 and e["gross_short"] == 100.0
    assert e["net"] == 0.0 and e["tilt"] == 0.0
    assert exposure_warning(e) is None


def test_tilt_ratio_math():
    # gross_long 150, gross_short 50 -> net 100, tilt = 100/200 = 0.5, long_share 0.75
    pos = [_pos("BTCUSDT", "long", 0.15, 1000.0), _pos("ETHUSDT", "short", 0.5, 100.0)]
    e = book_exposure(pos, {"BTCUSDT": 1000.0, "ETHUSDT": 100.0}, equity=10_000.0)
    assert e["gross_long"] == 150.0 and e["gross_short"] == 50.0
    assert e["net"] == 100.0 and round(e["tilt"], 4) == 0.5 and e["long_share"] == 0.75


def test_warning_is_symmetric_long_vs_short():
    # the SAME imbalance magnitude must nag equally whether net-long or net-short
    long_book = book_exposure([_pos("B", "long", 0.15, 1000.0), _pos("E", "short", 0.5, 100.0)],
                              {"B": 1000.0, "E": 100.0}, equity=10_000.0)
    short_book = book_exposure([_pos("B", "short", 0.15, 1000.0), _pos("E", "long", 0.5, 100.0)],
                               {"B": 1000.0, "E": 100.0}, equity=10_000.0)
    wl, ws = exposure_warning(long_book), exposure_warning(short_book)
    assert wl is not None and ws is not None  # both tilted books warn
    assert "LONG" in wl and "SHORT" in ws     # each names its own tilt
    assert long_book["tilt"] == short_book["tilt"]  # identical magnitude -> identical pressure


def test_mild_tilt_below_threshold_no_warning():
    # gross_long 110, gross_short 90 -> tilt 0.1 (< 0.30) -> tolerated, no nag
    pos = [_pos("B", "long", 0.11, 1000.0), _pos("E", "short", 0.9, 100.0)]
    e = book_exposure(pos, {"B": 1000.0, "E": 100.0}, equity=10_000.0)
    assert round(e["tilt"], 4) == 0.1 and exposure_warning(e) is None


def test_mark_falls_back_to_entry_when_price_missing():
    e = book_exposure([_pos("BTCUSDT", "long", 0.1, 1000.0)], {}, equity=10_000.0)  # no price
    assert e["gross_long"] == 100.0  # used entry as the mark


# ---- RISK-AWARE nag (the cycle-19 flag fix): de-risked legs are not a directional bet ----

def test_profit_locked_short_not_risk_bearing():
    # short with stop BELOW entry (trailed past breakeven) = profit-locked, NO downside risk
    e = book_exposure([_pos("SOL", "short", 1.0, 100.0, stop=98.0)], {"SOL": 95.0}, equity=10_000.0)
    assert e["gross_short"] > 0           # full notional still shown
    assert e["gross_short_rb"] == 0.0     # but it carries no risk
    assert e["tilt_rb"] == 0.0            # so the risk-bearing tilt is flat


def test_fully_derisked_net_short_book_does_not_nag():
    # the exact cycle-19 case: TWO shorts net-short by notional, but BOTH stops at/past breakeven ->
    # zero directional risk -> the nag must stay SILENT even though notional tilt is 1.0
    pos = [_pos("XRP", "short", 4000.0, 1.28, stop=1.27),   # stop below entry = profit-locked
           _pos("SOL", "short", 50.0, 80.0, stop=80.0)]      # stop at entry = breakeven
    e = book_exposure(pos, {"XRP": 1.26, "SOL": 79.0}, equity=10_000.0)
    assert e["tilt"] == 1.0               # notional view: fully net-short
    assert e["gross_rb"] == 0.0 and e["tilt_rb"] == 0.0   # risk view: nothing at risk
    assert exposure_warning(e) is None    # -> correctly silent (no real directional bet)


def test_one_risk_bearing_short_still_nags():
    # one risk-free short + one genuine risk-bearing short -> still a net-short directional bet -> nag
    pos = [_pos("XRP", "short", 4000.0, 1.28, stop=1.27),   # risk-free
           _pos("SOL", "short", 50.0, 80.0, stop=84.0)]      # risk-bearing (stop above entry)
    e = book_exposure(pos, {"XRP": 1.26, "SOL": 79.0}, equity=10_000.0)
    w = exposure_warning(e)
    assert w is not None and "SHORT" in w  # the genuine directional short still nags
    assert e["gross_short_rb"] > 0 and e["tilt_rb"] == 1.0


def test_risk_bearing_symmetry_long_vs_short():
    # a genuinely net-LONG-at-risk book nags to add shorts, mirror of the short case
    long_book = book_exposure([_pos("B", "long", 0.2, 1000.0, stop=950.0)], {"B": 1010.0}, 10_000.0)
    w = exposure_warning(long_book)
    assert w is not None and "LONG" in w


def test_reduce_lowers_book_exposure(tmp_path):
    # trimming a leg halves its notional, so gross drops (notional = qty * mark)
    from tests.test_orchestration import _seed_holding, _settings
    from futures_fund.orchestration import gate_execute_step
    import datetime as dt
    state_dir, memory_dir, ex = _seed_holding(tmp_path)  # one ETH long, qty 1.0
    full = gate_execute_step(ex, _settings(), state_dir, memory_dir,
                             now=dt.datetime(2026, 3, 1, tzinfo=dt.UTC), cycle_no=2,
                             proposals=[], management=[{"symbol": "ETHUSDT", "action": "hold"}])
    gross_before = full["exposure"]["gross"]
    state_dir2, memory_dir2, ex2 = _seed_holding(tmp_path / "b")
    trimmed = gate_execute_step(
        ex2, _settings(), state_dir2, memory_dir2, now=dt.datetime(2026, 3, 1, tzinfo=dt.UTC),
        cycle_no=2, proposals=[],
        management=[{"symbol": "ETHUSDT", "action": "reduce", "reduce_fraction": 0.5}])
    assert trimmed["exposure"]["gross"] < gross_before  # half the qty -> ~half the gross

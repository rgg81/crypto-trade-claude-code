import datetime as dt
import json
from datetime import UTC, datetime

from futures_fund.monitor import check_positions, notify, position_marks
from futures_fund.state import Position


def _pos(symbol, liq=1.0):
    return Position(symbol=symbol, direction="long", qty=1.0, entry=10.0, stop=9.0,
                    take_profits=[12.0], leverage=2.0, margin=5.0, liq_price=liq,
                    opened_cycle=1, opened_ts=dt.datetime(2026, 2, 1, tzinfo=UTC))


class _FakeEx:
    def __init__(self, raw_to_unified, marks, fail=()):
        self._r2u = raw_to_unified
        self._marks = marks
        self._fail = set(fail)

    def unified_for_raw(self, raw):
        return self._r2u.get(raw)

    def mark_price(self, unified):
        if unified in self._fail:
            raise RuntimeError("no data this tick")
        return self._marks[unified]


def test_position_marks_prices_every_holding_not_config_symbols():
    # INJ is held but would NOT be in the BTC/ETH config fallback — it MUST still be priced.
    positions = [_pos("ETHUSDT"), _pos("INJUSDT")]
    ex = _FakeEx({"ETHUSDT": "ETH/USDT:USDT", "INJUSDT": "INJ/USDT:USDT"},
                 {"ETH/USDT:USDT": 2010.0, "INJ/USDT:USDT": 6.6})
    marks, unpriced = position_marks(ex, positions)
    assert marks == {"ETHUSDT": 2010.0, "INJUSDT": 6.6}
    assert unpriced == []


def test_position_marks_surfaces_unmappable_holding():
    ex = _FakeEx({}, {})  # unified_for_raw -> None (delisted/unknown)
    marks, unpriced = position_marks(ex, [_pos("DEADUSDT")])
    assert marks == {} and unpriced == ["DEADUSDT"]


def test_position_marks_surfaces_transient_price_failure():
    ex = _FakeEx({"INJUSDT": "INJ/USDT:USDT"}, {}, fail=["INJ/USDT:USDT"])
    marks, unpriced = position_marks(ex, [_pos("INJUSDT")])
    assert marks == {} and unpriced == ["INJUSDT"]


def test_alerts_when_mark_near_liquidation():
    positions = [{"symbol": "BTCUSDT", "liq_price": 82.0}]
    out = check_positions(positions, {"BTCUSDT": 88.0}, equity=10_000.0, peak_equity=10_000.0,
                          liq_buffer=0.10)
    assert any("liquidation" in a for a in out["alerts"])
    assert out["should_halt"] is False


def test_no_alert_when_far_from_liquidation():
    positions = [{"symbol": "BTCUSDT", "liq_price": 50.0}]
    out = check_positions(positions, {"BTCUSDT": 100.0}, equity=10_000.0, peak_equity=10_000.0)
    assert out["alerts"] == [] and out["should_halt"] is False


def test_drawdown_halt():
    out = check_positions([], {}, equity=8_400.0, peak_equity=10_000.0, dd_halt=0.15)  # -16%
    assert out["should_halt"] is True
    assert out["drawdown"] > 0.15


def test_notify_appends_jsonl(tmp_path):
    notify(tmp_path, "circuit breaker tripped", ts=datetime(2026, 5, 1, tzinfo=UTC))
    raw = (tmp_path / "notifications.jsonl").read_text().splitlines()
    lines = [json.loads(x) for x in raw if x.strip()]
    assert lines[0]["message"] == "circuit breaker tripped"

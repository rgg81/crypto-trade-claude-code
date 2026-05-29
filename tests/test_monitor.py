import json
from datetime import UTC, datetime

from futures_fund.monitor import check_positions, notify


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

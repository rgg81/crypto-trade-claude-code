from futures_fund.config import Settings
from futures_fund.live_gate import live_allowed


def _sc(status):
    return {"graduation": {"status": status}}


def test_live_blocked_when_flag_off():
    assert live_allowed(Settings(live=False), _sc("graduated")) is False


def test_live_blocked_when_not_graduated():
    assert live_allowed(Settings(live=True), _sc("not_yet")) is False
    assert live_allowed(Settings(live=True), _sc("failed")) is False


def test_live_allowed_only_when_flag_on_and_graduated():
    assert live_allowed(Settings(live=True), _sc("graduated")) is True


def test_live_blocked_on_missing_scorecard_fields():
    assert live_allowed(Settings(live=True), {}) is False

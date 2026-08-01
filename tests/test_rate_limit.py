"""Tests for the Binance IP-weight headroom guard (cy317).

Why this exists: on 2026-08-01 the standing `fetch_time()` probe PASSED and the very next
call (the scout's `load_markets`) took a FRESH 418 IP ban. Because the probe succeeded the
ban was not already active — the scout's own burst created it. A weight-1 probe can only
detect an ALREADY-ACTIVE ban; it is blind to a DEPLETED-WEIGHT state, which is exactly the
condition that makes the next heavy call trip a new ban. Live sampling then showed the
used-weight counter swinging 362 -> 1405 while this process made only weight-1 calls, i.e.
a CO-CONSUMER shares the IP's 2400/min budget (confirming the cy292 shared-IP hypothesis).

So the guard asks "is there HEADROOM?" rather than "is there a ban?".
"""
from __future__ import annotations

import pytest

from futures_fund.rate_limit import (
    WEIGHT_LIMIT_PER_MIN,
    HeadroomTimeout,
    used_weight,
    wait_for_headroom,
)


class FakeClient:
    """Minimal ccxt stand-in: each fetch_time() pops the next scripted weight reading."""

    def __init__(self, weights, *, headers_key="x-mbx-used-weight-1m"):
        self._weights = list(weights)
        self._headers_key = headers_key
        self.calls = 0
        self.last_response_headers = {}

    def fetch_time(self):
        self.calls += 1
        w = self._weights.pop(0) if self._weights else self._weights_default()
        if w is None:
            self.last_response_headers = {}
        else:
            # real Binance casing is mixed; the reader must be case-insensitive
            self.last_response_headers = {"X-MBX-Used-Weight-1M": str(w)}
        return 1785583617958

    def _weights_default(self):
        return 0


def test_used_weight_reads_the_header_case_insensitively():
    c = FakeClient([1405])
    c.fetch_time()
    assert used_weight(c) == 1405


def test_used_weight_returns_none_when_header_absent():
    """No header must NOT be reported as zero usage — that would be a false all-clear."""
    c = FakeClient([None])
    c.fetch_time()
    assert used_weight(c) is None


def test_used_weight_returns_none_on_unparseable_header():
    c = FakeClient([1])
    c.fetch_time()
    c.last_response_headers = {"x-mbx-used-weight-1m": "not-a-number"}
    assert used_weight(c) is None


def test_used_weight_handles_missing_attribute():
    class Bare:
        pass

    assert used_weight(Bare()) is None


def test_wait_for_headroom_returns_immediately_when_budget_is_free():
    c = FakeClient([120])
    got = wait_for_headroom(c, threshold=700, sleep=lambda s: None)
    assert got == 120
    assert c.calls == 1


def test_wait_for_headroom_polls_until_the_window_clears():
    """The live pattern: depleted, depleted, then a clear window."""
    slept = []
    c = FakeClient([1405, 1264, 599])
    got = wait_for_headroom(c, threshold=700, sleep=slept.append)
    assert got == 599
    assert c.calls == 3
    assert len(slept) == 2  # slept between polls, not after success


def test_wait_for_headroom_raises_when_no_window_appears():
    c = FakeClient([2000] * 10)
    with pytest.raises(HeadroomTimeout) as e:
        wait_for_headroom(c, threshold=700, max_polls=4, sleep=lambda s: None)
    assert c.calls == 4
    assert "2000" in str(e.value)  # surfaces the last reading, fail-loud


def test_unknown_weight_is_treated_as_no_headroom_fail_closed():
    """An absent header must be FAIL-CLOSED: we cannot prove headroom, so we do not claim it."""
    c = FakeClient([None, None, 300])
    got = wait_for_headroom(c, threshold=700, sleep=lambda s: None)
    assert got == 300
    assert c.calls == 3


def test_unknown_weight_times_out_rather_than_passing_blind():
    c = FakeClient([None] * 10)
    with pytest.raises(HeadroomTimeout):
        wait_for_headroom(c, threshold=700, max_polls=3, sleep=lambda s: None)


def test_threshold_defaults_leave_real_room_under_the_binance_cap():
    """The scout's burst (load_markets + fetch_tickers) needs a few hundred weight of room."""
    from futures_fund.rate_limit import DEFAULT_THRESHOLD

    assert WEIGHT_LIMIT_PER_MIN == 2400
    assert 0 < DEFAULT_THRESHOLD <= WEIGHT_LIMIT_PER_MIN // 2


def test_guard_is_disabled_by_a_non_positive_threshold():
    """Escape hatch: threshold 0 must make guard() a no-op without touching the network."""
    from futures_fund.rate_limit import guard

    said = []
    assert guard(object(), threshold=0, echo=said.append) is None
    assert guard(object(), threshold=-1, echo=said.append) is None
    assert said == []


def test_guard_reports_the_observed_weight(monkeypatch):
    import futures_fund.exchange as exchange_mod
    from futures_fund.rate_limit import guard

    monkeypatch.setattr(exchange_mod, "build_ccxt", lambda s: FakeClient([310]))
    said = []
    assert guard(object(), threshold=700, echo=said.append) == 310
    assert "310" in said[0] and "2400" in said[0]


def test_wait_for_headroom_never_raises_on_a_transient_fetch_error():
    """A blip while polling must not abort the cycle — it just counts as 'unknown'."""

    class Flaky(FakeClient):
        def fetch_time(self):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("transient socket blip")
            self.last_response_headers = {"x-mbx-used-weight-1m": "250"}
            return 1

    c = Flaky([])
    got = wait_for_headroom(c, threshold=700, sleep=lambda s: None)
    assert got == 250
    assert c.calls == 2

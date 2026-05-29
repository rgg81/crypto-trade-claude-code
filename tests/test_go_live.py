from futures_fund.config import Settings
from futures_fund.live_exec import LiveExecutor
from futures_fund.live_gate import live_allowed


class _Boom:
    def create_order(self, *a, **k):
        raise AssertionError("no order may be placed when live is not allowed / not confirmed")


def test_kill_switch_drill_no_orders_when_not_allowed():
    # not graduated -> live_allowed False -> the operator must not place orders
    assert live_allowed(Settings(live=True), {"graduation": {"status": "not_yet"}}) is False


def test_executor_refuses_without_confirm_even_if_allowed():
    # even when live_allowed would be True, place_book still requires confirm_live
    ex = LiveExecutor(_Boom())
    try:
        ex.place_book([{"symbol": "BTCUSDT", "type": "market", "side": "buy", "amount": 0.1}],
                      confirm_live=False)
        raised = False
    except RuntimeError:
        raised = True
    assert raised  # double-gate held: no create_order reached _Boom


def test_runbook_documents_double_gate_and_kill_switch():
    from pathlib import Path
    rb = Path("README.md").read_text()
    assert "Going live" in rb or "go-live" in rb.lower()
    assert "graduated" in rb and ("HALT" in rb or "kill" in rb.lower())

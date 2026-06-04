import pytest
from pydantic import ValidationError

from futures_fund.contracts import (
    AgentProposal,
    AnalystReport,
    Candidate,
    ResearchPlan,
    rating_to_direction,
    to_trade_proposal,
)
from futures_fund.models import TradeProposal


def test_candidate_rejects_bad_lean():
    with pytest.raises(ValidationError):
        Candidate(symbol="BTC/USDT:USDT", lean="sideways", rationale="x", score=0.5)


def test_rating_to_direction_maps_five_tiers():
    assert rating_to_direction("strong_long") == "long"
    assert rating_to_direction("long") == "long"
    assert rating_to_direction("short") == "short"
    assert rating_to_direction("strong_short") == "short"
    assert rating_to_direction("flat") is None


def test_research_plan_requires_falsifiable_prediction():
    with pytest.raises(ValidationError):
        ResearchPlan(symbol="BTCUSDT", rating="long", confidence=0.7, thesis="up only")


def test_analyst_report_allows_extra_signal_fields():
    r = AnalystReport(agent="technical", symbol="BTCUSDT", stance="bullish", confidence=0.6,
                      signals={"rsi": 62.0}, extra_note="breakout")
    assert r.signals["rsi"] == 62.0
    assert r.model_dump()["extra_note"] == "breakout"


def test_to_trade_proposal_maps_fields_and_injects_funding():
    ap = AgentProposal(symbol="BTCUSDT", direction="long", entry=100.0, stop=95.0,
                       take_profits=[115.0], atr=2.0, confidence=0.7, horizon_hours=8,
                       rationale="trend + funding tailwind")
    tp = to_trade_proposal(ap, funding_rate=0.0001)
    assert isinstance(tp, TradeProposal)
    assert tp.symbol == "BTCUSDT" and tp.direction == "long"
    assert tp.funding_rate == 0.0001
    assert tp.risk_per_unit == pytest.approx(5.0)


def test_to_trade_proposal_threads_risk_mult():
    # the optional per-trade risk_mult must flow AgentProposal -> TradeProposal (default 1.0)
    from futures_fund.contracts import AgentProposal, to_trade_proposal
    ap = AgentProposal(symbol="BTCUSDT", direction="short", entry=100.0, stop=105.0,
                       take_profits=[90.0], atr=2.0, confidence=0.6, risk_mult=0.5)
    tp = to_trade_proposal(ap, funding_rate=0.0001)
    assert tp.risk_mult == 0.5
    # default path unchanged
    ap2 = AgentProposal(symbol="BTCUSDT", direction="long", entry=100.0, stop=95.0,
                        take_profits=[110.0], atr=2.0, confidence=0.6)
    assert to_trade_proposal(ap2, 0.0001).risk_mult == 1.0

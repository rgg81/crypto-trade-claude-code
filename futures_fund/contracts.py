from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from futures_fund.models import Direction, TradeProposal

Lean = Literal["long", "short", "watch"]
Rating = Literal["strong_long", "long", "flat", "short", "strong_short"]
Stance = Literal["bullish", "bearish", "neutral"]


class Candidate(BaseModel):
    symbol: str                       # ccxt unified symbol, e.g. BTC/USDT:USDT
    lean: Lean
    rationale: str
    score: float = Field(ge=0.0, le=1.0)
    correlation_group: str | None = None


class WatcherOutput(BaseModel):
    candidates: list[Candidate] = Field(default_factory=list)


class AnalystReport(BaseModel):
    model_config = ConfigDict(extra="allow")  # tolerate agent-specific signal fields
    agent: str                        # e.g. 'technical', 'derivatives', 'news', 'sentiment'
    symbol: str
    stance: Stance
    confidence: float = Field(ge=0.0, le=1.0)
    key_points: list[str] = Field(default_factory=list)
    signals: dict = Field(default_factory=dict)


class ResearchPlan(BaseModel):
    symbol: str
    rating: Rating
    confidence: float = Field(ge=0.0, le=1.0)
    thesis: str
    falsifiable_prediction: str


class AgentProposal(BaseModel):
    symbol: str                       # raw exchange id, e.g. BTCUSDT (matches SymbolSpec.symbol)
    direction: Direction
    entry: float
    stop: float
    take_profits: list[float]
    atr: float
    confidence: float = Field(ge=0.0, le=1.0)
    horizon_hours: float = 4.0
    rationale: str = ""
    falsifiable_prediction: str = ""  # from the RM plan -> journaled -> tested at HOLD/CLOSE
    confirmation: bool = True         # QuantAgent-style confirmation trigger


_RATING_DIRECTION: dict[str, Direction] = {
    "strong_long": "long", "long": "long", "short": "short", "strong_short": "short",
}


def rating_to_direction(rating: Rating) -> Direction | None:
    """5-tier research rating -> trade direction. 'flat' -> None (no trade)."""
    return _RATING_DIRECTION.get(rating)


def to_trade_proposal(ap: AgentProposal, funding_rate: float) -> TradeProposal:
    """Convert an agent's structured proposal into the A1 TradeProposal the risk gate consumes."""
    return TradeProposal(
        symbol=ap.symbol, direction=ap.direction, entry=ap.entry, stop=ap.stop,
        take_profits=ap.take_profits, atr=ap.atr, confidence=ap.confidence,
        horizon_hours=ap.horizon_hours, funding_rate=funding_rate,
    )

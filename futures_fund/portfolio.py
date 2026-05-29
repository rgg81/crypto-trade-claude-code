from __future__ import annotations

from futures_fund.models import PortfolioHealth
from futures_fund.portfolio_risk import position_risk
from futures_fund.state import Position


def unrealized_pnl(position: Position, mark: float) -> float:
    if position.direction == "long":
        return position.qty * (mark - position.entry)
    return position.qty * (position.entry - mark)


def total_equity(balance: float, positions: list[Position], prices: dict[str, float]) -> float:
    """Wallet balance + unrealized PnL of open positions (skips positions with no price)."""
    upnl = 0.0
    for p in positions:
        mark = prices.get(p.symbol)
        if mark is not None:
            upnl += unrealized_pnl(p, mark)
    return balance + upnl


def open_heat(positions: list[Position], equity: float) -> float:
    """Sum of per-position stop-out risk as a fraction of equity (reuses A1 position_risk)."""
    return sum(position_risk(p.qty, p.entry, p.stop, equity) for p in positions)


def portfolio_health(
    balance: float, peak_equity: float, positions: list[Position],
    prices: dict[str, float], recent_hit_rate: float = 0.5,
) -> PortfolioHealth:
    """Compute A1's PortfolioHealth from live marks, raising the high-water mark if exceeded."""
    equity = total_equity(balance, positions, prices)
    return PortfolioHealth(
        equity=equity,
        peak_equity=max(peak_equity, equity),
        open_heat=open_heat(positions, equity) if equity > 0 else 0.0,
        recent_hit_rate=recent_hit_rate,
    )

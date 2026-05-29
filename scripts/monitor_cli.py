"""Between-tick light risk monitor (run on a faster ~15-30min cron than the 4h cycle).
Checks liquidation distance + drawdown; trips the HALT flag and notifies if breached.

    uv run python scripts/monitor_cli.py
"""
from __future__ import annotations

import json
from datetime import UTC, datetime

from futures_fund.config import load_settings
from futures_fund.exchange import FuturesExchange
from futures_fund.monitor import check_positions, notify
from futures_fund.portfolio import total_equity
from futures_fund.state import load_account, load_positions, set_halt


def main() -> None:
    settings = load_settings()
    account = load_account("state", settings.account_size_usdt)
    positions = load_positions("state")
    ex = FuturesExchange.from_settings(settings)
    marks = {p.symbol: ex.mark_price(s) for s in settings.symbols
             for p in positions if ex.symbol_spec(s).symbol == p.symbol}
    equity = total_equity(account.balance, positions, marks)
    pos_dicts = [{"symbol": p.symbol, "liq_price": p.liq_price} for p in positions]
    now = datetime.now(UTC)
    out = check_positions(pos_dicts, marks, equity=equity, peak_equity=account.peak_equity)
    if out["should_halt"]:
        set_halt("state", True, reason="monitor: drawdown halt")
        notify("state", f"HALT tripped by monitor: {out['alerts']}", now)
    elif out["alerts"]:
        notify("state", f"risk alerts: {out['alerts']}", now)
    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    main()

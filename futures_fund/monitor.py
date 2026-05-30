from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path


def check_positions(positions: list[dict], marks: dict[str, float], *, equity: float,
                    peak_equity: float, liq_buffer: float = 0.10, dd_halt: float = 0.15) -> dict:
    """Cheap between-tick safety check: alert when any position's mark is within `liq_buffer`
    of its liquidation price, and signal HALT when drawdown-from-peak exceeds `dd_halt`."""
    alerts: list[str] = []
    for p in positions:
        mark = marks.get(p["symbol"])
        if mark is None or mark <= 0:
            continue
        dist = abs(mark - p["liq_price"]) / mark
        if dist <= liq_buffer:
            alerts.append(
                f"{p['symbol']} within {dist:.1%} of liquidation"
                f" (mark {mark}, liq {p['liq_price']})"
            )
    drawdown = (peak_equity - equity) / peak_equity if peak_equity > 0 else 0.0
    should_halt = drawdown >= dd_halt
    if should_halt:
        alerts.append(f"drawdown {drawdown:.1%} >= halt threshold {dd_halt:.0%}")
    return {"alerts": alerts, "should_halt": should_halt, "drawdown": drawdown}


def position_marks(exchange, positions) -> tuple[dict[str, float], list[str]]:
    """Mark price for every OPEN position, keyed by its raw symbol — derived from the positions
    THEMSELVES, not a config symbol list, so a held symbol outside this cycle's dynamic universe
    is still monitored (the prior bug skipped any holding not in settings.symbols). Returns
    (marks, unpriced): `unpriced` are holdings we could not price this tick (delisted/unmappable
    or a transient data error) and are therefore NOT watched — the caller must surface them."""
    marks: dict[str, float] = {}
    unpriced: list[str] = []
    for p in positions:
        unified = exchange.unified_for_raw(p.symbol)
        mark = None
        if unified is not None:
            try:
                mark = exchange.mark_price(unified)
            except Exception:  # noqa: BLE001 — transient data error -> treat as unpriceable
                mark = None
        if mark is not None and mark > 0:
            marks[p.symbol] = float(mark)
        else:
            unpriced.append(p.symbol)
    return marks, unpriced


def notify(state_dir, message: str, ts: datetime) -> None:
    """Append a notification (the 'notify' half of auto-execute+notify). A real channel
    (email/Telegram) can tail this file."""
    p = Path(state_dir) / "notifications.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as f:
        f.write(json.dumps({"ts": ts.isoformat(), "message": message}) + "\n")

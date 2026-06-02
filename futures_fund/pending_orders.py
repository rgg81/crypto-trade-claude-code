"""Conditional / trigger orders — resting intents that let the desk act on its own analysis
across cycles instead of "wait and re-decide by hand" (which lost the whole SUI move: it fell
straight down, never bouncing to the 0.887 trigger that only lived in the orchestrator's head).

A trigger fires off the latest COMPLETED 4h bar (hybrid by kind: stop-entry on a CLOSE beyond the
level = a confirmed break; limit-entry on a LOW/HIGH TOUCH = a pullback fill), then becomes a
NORMAL proposal at the trigger price and routes through the EXACT existing gate (RR>=2, heat cap,
1%-sizing, liq) re-checked against the LIVE regime — no privileged path. A FIRED trigger is already
a confirmed break, so it is exempt from the gate's counter-regime confirmation transform.
"""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

from pydantic import BaseModel, Field


class PendingOrder(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    symbol: str                      # RAW exchange id (BTCUSDT), matching AgentProposal/Position
    direction: str                   # 'long' | 'short'
    kind: str                        # 'stop_entry' | 'limit_entry'
    trigger_level: float
    stop: float
    take_profits: list[float] = Field(default_factory=list)
    atr: float = 0.0
    falsifiable_prediction: str = ""
    rationale: str = ""
    confidence: float = 0.5
    created_cycle: int = 0
    expires_cycle: int = 0


def _store(state_dir) -> Path:
    return Path(state_dir) / "pending_orders.json"


def load_pending_orders(state_dir) -> list[PendingOrder]:
    """Missing file -> []. Skips per-order malformed records; never raises (corrupt store ==
    no armed triggers, fail-safe)."""
    p = _store(state_dir)
    if not p.exists():
        return []
    try:
        raw = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError, ValueError):
        return []
    out = []
    for rec in raw if isinstance(raw, list) else []:
        try:
            out.append(PendingOrder.model_validate(rec))
        except Exception:  # noqa: BLE001 — drop a malformed order, keep the rest
            continue
    return out


def save_pending_orders(state_dir, orders: list[PendingOrder]) -> None:
    p = _store(state_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps([o.model_dump(mode="json") for o in orders], indent=2))
    os.replace(tmp, p)


def _key(o: PendingOrder) -> tuple:
    return (o.symbol, o.direction, o.kind)


def upsert_triggers(orders: list[PendingOrder], new_triggers: list[PendingOrder]) -> list[PendingOrder]:
    """Append-or-REPLACE by (symbol, direction, kind); dedupe the new batch among itself (last
    wins) so a re-stated trigger never duplicates."""
    merged = {_key(o): o for o in orders}
    for nt in new_triggers:
        merged[_key(nt)] = nt
    return list(merged.values())


def fired_to_proposal(o: PendingOrder) -> dict:
    """A fired trigger becomes a normal AgentProposal at the TRIGGER price (favorable paper fill).
    It then competes in the same gate (RR/heat/sizing/liq) as fresh opens — but, being an already
    confirmed break, it is EXEMPT from the counter-regime confirmation transform (not re-armed)."""
    return {"symbol": o.symbol, "direction": o.direction, "entry": o.trigger_level,
            "stop": o.stop, "take_profits": o.take_profits, "atr": o.atr,
            "confidence": o.confidence, "falsifiable_prediction": o.falsifiable_prediction,
            "rationale": f"[trigger:{o.kind}] {o.rationale}"}


def _wrong_side_stop(o: PendingOrder) -> bool:
    # a long's stop must be BELOW the entry/trigger; a short's ABOVE. Inverted => reject.
    return (o.direction == "long" and o.stop >= o.trigger_level) or \
           (o.direction == "short" and o.stop <= o.trigger_level)


def check_pending_orders(state_dir, bars_by_symbol: dict, cycle_no: int,
                         held_symbols=frozenset()) -> tuple[list, list, list]:
    """Evaluate every armed order against the latest COMPLETED 4h bar (RAW-keyed). Returns
    (fired, expired, remaining) — disjoint. FIRE precedes EXPIRY. Held-symbol, knife-guarded, and
    wrong-side orders are CONSUMED (in none of the three lists -> removed from the store). No-bar
    orders are UNEVALUABLE and stay in `remaining` (still pending) unless they also expire."""
    fired, expired, remaining = [], [], []
    for o in load_pending_orders(state_dir):
        if o.symbol in held_symbols:
            continue  # no stacking against a live position; the team flips via holdings CLOSE
        bar = bars_by_symbol.get(o.symbol)
        fire = consumed = False
        if bar is not None and not _wrong_side_stop(o):
            close, low, high = bar.get("close"), bar.get("low"), bar.get("high")
            if o.kind == "stop_entry":  # confirmed break on the bar CLOSE
                fire = (o.direction == "short" and close is not None and close < o.trigger_level) or \
                       (o.direction == "long" and close is not None and close > o.trigger_level)
            else:                        # limit_entry: TOUCH of the level
                if o.direction == "long" and low is not None and low <= o.trigger_level:
                    if low <= o.stop:    # knife guard: bar tagged trigger AND stop in one bar
                        consumed = True
                    else:
                        fire = True
                elif o.direction == "short" and high is not None and high >= o.trigger_level:
                    if high >= o.stop:
                        consumed = True
                    else:
                        fire = True
        elif bar is not None and _wrong_side_stop(o):
            consumed = True              # inverted geometry -> drop, never re-arm
        if fire:                          # FIRE wins over expiry
            fired.append(o)
        elif consumed:
            continue                      # knife / wrong-side -> removed
        elif cycle_no >= o.expires_cycle:
            expired.append(o)
        else:
            remaining.append(o)           # unfired (incl. no-bar unevaluable) stays armed
    return fired, expired, remaining

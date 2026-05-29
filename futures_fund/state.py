from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field

from futures_fund.models import Direction


class Position(BaseModel):
    symbol: str
    direction: Direction
    qty: float
    entry: float
    stop: float
    take_profits: list[float] = Field(default_factory=list)
    leverage: float
    margin: float
    liq_price: float
    opened_cycle: int
    opened_ts: datetime
    decision_id: str | None = None


class AccountState(BaseModel):
    balance: float          # realized USDT wallet balance
    peak_equity: float      # peak of total equity (balance + unrealized) ever seen
    halt: bool = False
    halt_reason: str = ""
    updated_ts: datetime | None = None


def _account_path(state_dir) -> Path:
    return Path(state_dir) / "account.json"


def _positions_path(state_dir) -> Path:
    return Path(state_dir) / "positions.json"


def load_account(state_dir, default_balance: float) -> AccountState:
    p = _account_path(state_dir)
    if p.exists():
        return AccountState.model_validate_json(p.read_text())
    return AccountState(balance=default_balance, peak_equity=default_balance)


def save_account(state_dir, account: AccountState) -> None:
    p = _account_path(state_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(account.model_dump_json(indent=2))


def load_positions(state_dir) -> list[Position]:
    p = _positions_path(state_dir)
    if not p.exists():
        return []
    raw = json.loads(p.read_text())
    return [Position.model_validate(r) for r in raw]


def save_positions(state_dir, positions: list[Position]) -> None:
    p = _positions_path(state_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps([json.loads(pos.model_dump_json()) for pos in positions], indent=2))


def is_halted(state_dir) -> bool:
    p = _account_path(state_dir)
    if not p.exists():
        return False
    return AccountState.model_validate_json(p.read_text()).halt


def set_halt(state_dir, halt: bool, reason: str = "") -> None:
    # operates on the persisted account; balance/peak default to 0 only if no account exists yet
    p = _account_path(state_dir)
    acct = (
        AccountState.model_validate_json(p.read_text())
        if p.exists()
        else AccountState(balance=0.0, peak_equity=0.0)
    )
    acct.halt = halt
    acct.halt_reason = reason if halt else ""
    save_account(state_dir, acct)

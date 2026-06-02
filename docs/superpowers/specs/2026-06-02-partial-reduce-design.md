# Partial-Reduce / Trim (v1) — Design Spec

**Date:** 2026-06-02
**Status:** Approved (brainstorming) — ready for implementation plan
**Feature:** Let the team bank a fraction of a winning position and keep a smaller runner, replacing the current tighter-full-position-stop approximation.

---

## Goal

Add a discretionary **partial-reduce** ("trim") capability so the Trader can bank part of a
winning position at mark and carry a reduced-size runner, instead of approximating "bank half"
with a tighter whole-position stop. Realized PnL on the banked fraction is credited to the wallet
immediately; the runner stays open with the *same* thesis (`decision_id`, `falsifiable_prediction`).

## Non-negotiable constraints (from CLAUDE.md)

- **Zero edits to protected modules.** `risk_gate`, `executor`, `exits`, `consolidation`, `policy`,
  `liquidation`, `sizing`, `cycle` are reused as-is. The protected `executor.close_at_mark` is
  called **read-only** on a temporary slice Position; nothing in a protected file is modified.
- **Market-neutral symmetry.** The trim is purely qty-based; PnL sign is handled by
  `close_at_mark`'s existing symmetric long/short branches. Every guard applies identically to both
  sides.
- **Never hand-edit state.** The reduce flows through the normal
  `gate_execute_step → execute_proposals → save_account/save_positions` path.
- **Full `uv run pytest` green** before any commit.

## Architecture

A reduce is a new holdings-review management action handled entirely in **non-protected** code:

1. `futures_fund/reduce.py` *(new module)* — one pure helper that splits a Position into a banked
   slice (closed via the protected `close_at_mark`) and a reduced runner.
2. `futures_fund/orchestration.py` — a new `action == "reduce"` branch in the holdings-review loop
   (`gate_execute_step`, lines 386–404) that calls the helper, credits `account.balance`, swaps the
   runner into `new_positions`, and records telemetry.
3. Agent-facing docs (`agents/trader.md`, `SKILL.md`) — document the new directive.

`gate_execute_step` loads `account` and `positions` **in memory** (orchestration.py:362–363), mutates
them in the management loop, then passes those same objects to `execute_proposals`
(orchestration.py:483–488), which reserves heat on the **post-reduce** qty and persists both at the
end (cycle.py:273–274). So the reduce credits balance and shrinks the runner *in memory* before
`execute_proposals` runs — no separate save, no protected-code change. Heat and exposure are linear
in qty and recomputed from scratch each cycle, so they auto-correct; a reduce (risk-decreasing) is
never gated.

## Directive schema (v1 — deliberately minimal)

A new entry in the `management` list of `proposals.json`:

```json
{"symbol": "ZECUSDT", "action": "reduce", "reduce_fraction": 0.5, "reason": "bank +2R half"}
```

- `action`: `"reduce"` (joins existing `"hold"`, `"close"`).
- `reduce_fraction`: float, **strictly `0 < f < 1`**. No upper cap (the dust→full-close guard below
  already prevents a near-total trim from stranding an untradeable runner).
- `reason`: free text (telemetry/journal), as for hold/close.

Validation lives in the orchestration loop as an explicit guard (management directives are raw dicts
today; we keep that style rather than refactoring hold/close into a model). A malformed reduce
(missing/out-of-range `reduce_fraction`) is **dropped** — the directive is skipped and the position
is left untouched — never aborting the gate (mirrors the per-proposal `try/except` at
orchestration.py:468–479). A dropped reduce increments `report["reduce_dropped"]`.

## The helper — `futures_fund/reduce.py`

```python
def reduce_position(
    position: Position, mark: float, fraction: float, *,
    funding_rate: float, funding_events: int, slippage_bps: float,
    spec: SymbolSpec, pay_bnb: bool = False,
) -> ReduceResult:
    ...
```

`ReduceResult` (a small dataclass/NamedTuple) carries:
- `closed_trade: ClosedTrade | None` — the banked slice's realized result (only for `"reduced"`).
- `runner: Position | None` — the reduced-qty position to carry (only for `"reduced"`).
- `kind: Literal["reduced", "promote_full", "noop_dust"]`.

Behaviour:

1. `slice_qty = round_qty(fraction * position.qty, spec.step_size)` (floors to the lot step via the
   existing `orders.round_qty`). `remaining = position.qty - slice_qty`.
2. **Dust guards (symmetric) — pure signals, no close performed here:**
   - If `slice_qty <= 0` (fraction smaller than one lot) → `kind="noop_dust"`, return
     `(None, None)`; the position is left whole. Orchestration surfaces a warning.
   - Else if `remaining * mark < spec.min_notional` (the runner would be untradeable) → **promote to
     a full close (3b):** `kind="promote_full"`, `closed_trade=None`, `runner=None`. The helper does
     **not** close anything; orchestration routes the whole position through the existing
     `force_close` set so `execute_proposals` closes 100% with its normal accounting + journaling
     (no duplicated close logic).
3. Otherwise (`kind="reduced"`), build the slice: `slice_pos = position.model_copy(update={"qty": slice_qty})`.
4. `closed_trade = close_at_mark(slice_pos, mark, funding_rate=funding_rate,
   funding_events=funding_events, slippage_bps=slippage_bps, pay_bnb=pay_bnb)` — reuses the protected
   gross / exit-fee / **signed-funding** / slippage math on the slice qty only.
5. Build the runner: `runner = position.model_copy(update={"qty": remaining,
   "margin": position.margin * (remaining / position.qty)})`. `entry`, `direction`, `leverage`,
   `take_profits`, `decision_id`, **and `liq_price`** are unchanged. A proportional qty+margin
   reduction at constant leverage leaves the liquidation geometry unchanged, and the original
   (larger-notional) `liq_price` is conservative — never closer than reality for the smaller runner —
   so it is kept as-is. This means the helper needs no read-only import of the protected
   `liquidation` module at all; a smaller position is never less safe.
6. Return `(closed_trade, runner, kind="reduced")`.

The helper is pure (no I/O, no balance mutation) — fully unit-testable in isolation.

## Orchestration wiring — `gate_execute_step` holdings-review loop (orchestration.py:386–404)

Add a branch alongside the existing `close` / `hold+new_stop` branches:

```python
elif m and m.get("action") == "reduce":
    frac = _valid_reduce_fraction(m.get("reduce_fraction"))   # 0<f<1 else None
    mark = ctx.prices.get(p.symbol)
    if frac is None or mark is None:
        report["reduce_dropped"] = report.get("reduce_dropped", 0) + 1
        new_positions.append(p)            # leave the position untouched
        continue
    fr = ctx.fundings[ctx.raw_to_unified[p.symbol]]
    n_events = count_funding_events(p.opened_ts, now, int(fr.interval_hours))
    res = reduce_position(p, mark, frac, funding_rate=fr.current_rate,
                          funding_events=n_events, slippage_bps=_SLIPPAGE_BPS,
                          spec=ctx.specs_by_raw[p.symbol])
    if res.kind == "noop_dust":
        report.setdefault("warnings", []).append(f"reduce noop (dust) {p.symbol}")
        new_positions.append(p)            # leave whole
        continue
    if res.kind == "promote_full":
        force_close.add(p.symbol)          # let execute_proposals close 100% + journal it
        new_positions.append(p)
        report["actions"].append({"reduce": p.symbol, "fraction": frac, "full": True})
        continue
    # kind == "reduced": bank the slice, carry the runner
    account.balance += res.closed_trade.realized_pnl
    report["reduced"] = report.get("reduced", 0) + 1
    report["banked_pnl"] = report.get("banked_pnl", 0.0) + res.closed_trade.realized_pnl
    report["actions"].append({"reduce": p.symbol, "fraction": frac,
                              "pnl": res.closed_trade.realized_pnl, "full": False})
    new_positions.append(res.runner)       # carry the reduced runner
    continue
```

Notes:
- `_SLIPPAGE_BPS`, `count_funding_events`, `close_at_mark` are imported the same way the full-close
  path (cycle.py:14–15, 214–216) uses them, keeping the math identical.
- The reduce runs **before** `execute_proposals`, so the freed heat is credited to new opens that
  same cycle and the persisted book reflects the trim.
- A `promote_full` reduce reuses the existing `force_close` path — `execute_proposals` does the
  close, balance credit, and `patch_outcome`/`record_outcome`, so there is no duplicated full-close
  accounting and the decision is journaled exactly like any other close.

## Decision-journal continuity

A `"reduced"` trim banks realized PnL to `account.balance` and logs a `reduce` action, but does
**not** call `patch_outcome` / `record_outcome` — the thesis is still live and the runner keeps its
`decision_id` and `falsifiable_prediction`. The decision's terminal outcome is patched only when the
runner later fully closes (so hit-rate is not double-counted and the decision is not prematurely
marked closed). A `"promote_full"` reduce terminates the position via the existing `force_close`
path, so `execute_proposals` patches the outcome exactly like any normal close — no special-casing
in the reduce branch. The banked PnL is independently auditable via the per-cycle equity log
(already records equity each cycle) and the `report["actions"]` / `report["banked_pnl"]` telemetry.

## Halt behaviour

A reduce is risk-decreasing, so it is honoured on HALT exactly like a `close` (the holdings-review
loop runs regardless of halt; only `proposals` are zeroed on halt at orchestration.py:374–375). No
special handling — covered by a test.

## Telemetry (gate report)

New keys, next to the existing `trailed` / `closed`:
- `report["reduced"]` — count of executed trims.
- `report["banked_pnl"]` — summed realized PnL banked this cycle.
- `report["reduce_dropped"]` — count of malformed/unpriceable reduce directives dropped.
- `report["actions"]` gains `{"reduce": symbol, "fraction": f, "pnl": …, "full": bool}` entries.

## Files changed (all non-protected)

| File | Change |
|---|---|
| `futures_fund/reduce.py` *(new)* | `reduce_position()` + `ReduceResult`; the split + read-only `close_at_mark` reuse + dust/promote guards |
| `futures_fund/orchestration.py` | `action == "reduce"` branch in `gate_execute_step`; `_valid_reduce_fraction` helper; new report keys |
| `agents/trader.md` | new **Holdings management** section documenting `hold` / `close` / **`reduce`** (the charter currently documents no management schema) |
| `SKILL.md` (≈ line 53) | replace "v1 has no add/trim" with the `reduce` directive shape + semantics |

**Protected modules touched: none.**

## Testing (TDD)

New `tests/test_reduce.py` (unit, mirrors `tests/test_executor.py:50–63`):
- banked PnL equals `close_at_mark` on `slice_qty`, net of exit fee and **signed** funding (assert a
  funding-receiving trade is credited, not clamped).
- runner `qty` and `margin` shrink proportionally; `entry`/`leverage`/`decision_id` unchanged.
- **long and short symmetry** (same fraction banks the mirror PnL sign).
- dust → `promoted_full_close` when `remaining*mark < min_notional`; sub-lot fraction → `noop_dust`.
- `liq_price` recompute never moves the liquidation closer (runner is safer).

Integration:
- `tests/test_orchestration.py` (mirror `_seed_holding` + the hold/close/trail suite at 199–238):
  a `reduce 0.5` halves qty, banks PnL into `balance`, leaves the runner open, `report["reduced"]==1`;
  reduce works for a **short**; malformed `reduce_fraction` is dropped (`report["reduce_dropped"]`).
- `tests/test_gate_wiring.py`: `report["reduced"]` / `report["banked_pnl"]` telemetry; **reduce
  honoured on HALT**.
- `tests/test_exposure.py`: a trim lowers `gross`/`tilt` (notional = qty·mark).
- `tests/test_management_review.py`: a `reduce` directive round-trips through `management_review`
  unchanged (and a malformed one does not break the contract).

Full `uv run pytest` must pass.

## Out of scope (deliberate v1 simplifications)

- **Bank + trail the runner in one directive.** ✅ **SHIPPED in v2** (2026-06-02, after cycle 22 hit
  the limitation live): `reduce` now accepts an optional `new_stop` that trails the survivor's stop
  in the same directive, reusing the shared `_is_tighter_stop` guard (also used by the HOLD trail).
  *Original v1 note:* v1 `reduce` was a pure qty cut; to also move the runner's stop, the team used
  the existing `hold + new_stop` action in a subsequent review.
- **Automatic take-profit ladder** (auto-bank a fraction when TP1 hits). Would change the protected
  `exits`/`detect_exit` full-close path and risk double-banking with the existing full-TP trigger;
  out of scope.
- **RM-emitted reduce.** Partial-reduce is an execution-geometry decision, so it belongs to the
  Trader's schema; the RM stays direction/conviction-only (`agents/research_manager.md:19`).
- **`reduce_to_qty` / target-R sizing.** v1 uses `reduce_fraction` only.

## Open questions

None — design approved (3b confirmed; `new_stop` and fraction-cap dropped for simplicity).

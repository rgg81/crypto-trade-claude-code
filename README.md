# futures-fund — Operation TEMPEST

Autonomous multi-agent Binance USD-M perpetual futures desk (Claude Code skill).
See `docs/superpowers/specs/` for the design and `docs/superpowers/plans/` for build plans.

## Dev
```
uv sync
uv run pytest
uv run ruff check .
```

## Going live (real capital)

The desk is **paper-by-default** and **double-gated** before it can touch real money:

1. **Validate on testnet.** Put Binance USD-M **testnet** keys in `.env` (`BINANCE_KEY`/`BINANCE_SECRET`); keep `exchange.testnet: true`. Run cycles via `SKILL.md` and confirm orders/fills look right (`scripts/smoke_testnet.py`).
2. **Earn graduation.** Run ≥20–30 audited paper cycles. Check `uv run python scripts/go_live_check.py` — it must report `graduation.status == "graduated"` (positive OOS Sharpe, **DSR > 0.95**, beating buy-&-hold net of costs). Until then, live is refused.
3. **Enable live (explicit).** Only then set `live: true` in `config.yaml` and supply production keys. `LiveExecutor.place_book` *still* refuses unless called with `confirm_live=True`. Leverage is the gate's output; stops/TPs are always reduceOnly; margin is isolated.
4. **Schedule.** Full cycle every 4h (`cron`/scheduler → the `SKILL.md` orchestrator); the light risk monitor every ~15–30 min (`scripts/monitor_cli.py`) — it trips the **HALT** flag on a drawdown/liquidation-distance breach.

### Kill switch
`uv run python -c "from futures_fund.state import set_halt; set_halt('state', True, reason='manual kill')"` halts all new trading immediately; the cycle short-circuits at preflight while the exchange's resting reduceOnly stops keep protecting open positions. Clear with `set_halt('state', False)`.

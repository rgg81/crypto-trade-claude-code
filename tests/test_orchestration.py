import datetime as dt
from datetime import datetime

import numpy as np
import pandas as pd

from futures_fund.config import Settings
from futures_fund.contracts import AgentProposal
from futures_fund.orchestration import gate_execute_step, preflight_step, reflect_step, screen_step
from futures_fund.state import load_positions

UTC = dt.UTC

_RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel><item>
<title>BTC chops sideways</title><link>http://x/1</link>
<pubDate>Fri, 29 May 2026 14:20:32 +0000</pubDate></item></channel></rss>"""


class _Resp:
    def __init__(self, *, content=b"", payload=None, status=200):
        self.content = content
        self._p = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("http")

    def json(self):
        return self._p


class _HttpClient:
    def get(self, url, params=None, **kw):
        if "alternative.me" in url:
            return _Resp(payload={"data": [{"value": "30",
                                            "value_classification": "Fear",
                                            "timestamp": "1780012800"}]})
        return _Resp(content=_RSS)


class FakeExchange:
    def __init__(self, frames):
        self.frames = frames

    def symbol_spec(self, symbol):
        from futures_fund.models import MmrBracket, SymbolSpec
        return SymbolSpec(symbol="BTCUSDT", tick_size=0.01, step_size=0.001, min_notional=5.0,
                          mmr_brackets=[MmrBracket(notional_floor=0, notional_cap=1_000_000,
                                                   mmr=0.004, maint_amount=0.0, max_leverage=125)])

    def ohlcv(self, symbol, timeframe="4h", limit=500):
        return self.frames[symbol]

    def funding(self, symbol):
        from futures_fund.market_data import FundingInfo
        return FundingInfo(symbol=symbol, current_rate=0.0001,
                           next_funding_ts=dt.datetime(2026, 1, 1, tzinfo=UTC), interval_hours=8.0,
                           mark_price=float(self.frames[symbol]["close"].iloc[-1]),
                           index_price=float(self.frames[symbol]["close"].iloc[-1]))

    def open_interest_history(self, symbol, period="4h", limit=200):
        import pandas as pd
        return pd.DataFrame({"timestamp": pd.date_range("2026-01-01", periods=3, freq="4h",
                                                        tz="UTC"),
                             "oi_amount": [1., 1., 1.], "oi_value": [1e7, 1e7, 1e7]})

    def long_short_ratio(self, symbol, period="4h", limit=200):
        import pandas as pd
        return pd.DataFrame({"timestamp": pd.date_range("2026-01-01", periods=2, freq="4h",
                                                        tz="UTC"),
                             "long_short_ratio": [1.5, 1.6], "long_account": [0.6, 0.62],
                             "short_account": [0.4, 0.38]})


def _uptrend(n=60):
    rng = np.random.default_rng(7)
    close = 100.0 + 0.8 * np.arange(n) + rng.normal(0, 0.05, n)
    return pd.DataFrame({
        "timestamp": pd.date_range("2026-01-01", periods=n, freq="4h", tz="UTC"),
        "open": close, "high": close + 0.2, "low": close - 0.2, "close": close, "volume": 1.0,
    })


def _settings():
    return Settings(account_size_usdt=10_000.0, symbols=["BTC/USDT:USDT"], timeframe="4h")


def _post_crush(n=60):
    """A post-crash name: price ~1.0 but candle ranges ~2.0 -> ATR ~2.0 = ~200% of price
    (cy203 LAB: ATR 1.772 vs 0.998 price). Structurally un-tradeable: a >=0.6-ATR noise-safe
    stop forces a negative TP for RR>=2."""
    rng = np.random.default_rng(11)
    close = 1.0 + rng.normal(0, 0.02, n)
    return pd.DataFrame({
        "timestamp": pd.date_range("2026-01-01", periods=n, freq="4h", tz="UTC"),
        "open": close, "high": close + 1.0, "low": close - 1.0, "close": close, "volume": 1.0,
    })


class _MultiExchange(FakeExchange):
    """FakeExchange whose symbol_spec returns each symbol's own raw id, so a multi-symbol
    preflight briefs distinct raw ids instead of BTCUSDT for everything. Optionally carries a
    raw->unified map (unified_for_raw) so held positions fold into the universe."""
    def __init__(self, frames, raw_to_unified=None):
        super().__init__(frames)
        self._r2u = raw_to_unified or {}

    def unified_for_raw(self, raw_id):
        return self._r2u.get(raw_id)

    def symbol_spec(self, symbol):
        from futures_fund.models import MmrBracket, SymbolSpec
        raw = symbol.replace("/USDT:USDT", "USDT")
        return SymbolSpec(symbol=raw, tick_size=0.01, step_size=0.001, min_notional=5.0,
                          mmr_brackets=[MmrBracket(notional_floor=0, notional_cap=1_000_000,
                                                   mmr=0.004, maint_amount=0.0, max_leverage=125)])


def test_preflight_emits_context_with_briefs(tmp_path):
    ex = FakeExchange({"BTC/USDT:USDT": _uptrend()})
    ctx = preflight_step(ex, _settings(), tmp_path / "s", tmp_path / "m",
                         now=dt.datetime(2026, 3, 1, tzinfo=UTC), cycle_no=1,
                         http_client=_HttpClient())
    assert ctx["cycle"] == 1
    assert ctx["halted"] is False
    assert "BTC/USDT:USDT" in {b["symbol"] for b in ctx["briefs"]}
    assert ctx["briefs"][0]["regime"]  # brief carries the regime
    assert "equity" in ctx and ctx["equity"] > 0


def test_preflight_drops_post_crush_untradeable_candidate(tmp_path):
    # LAB is a post-crush crash artifact (ATR ~2.0 = ~200% of its ~1.0 price): clean stop/TP
    # geometry is mathematically impossible, so preflight must DROP it from candidate briefs and
    # surface it in dropped_untradeable (Rule 6 fail-loud). BTC (normal ATR) is kept.
    ex = _MultiExchange({"BTC/USDT:USDT": _uptrend(), "LAB/USDT:USDT": _post_crush()})
    settings = Settings(account_size_usdt=10_000.0,
                        symbols=["BTC/USDT:USDT", "LAB/USDT:USDT"], timeframe="4h")
    ctx = preflight_step(ex, settings, tmp_path / "s", tmp_path / "m",
                         now=dt.datetime(2026, 3, 1, tzinfo=UTC), cycle_no=1,
                         http_client=_HttpClient())
    briefed = {b["exchange_id"] for b in ctx["briefs"]}
    assert "BTCUSDT" in briefed          # tradeable name kept
    assert "LABUSDT" not in briefed      # post-crush candidate dropped
    dropped = {d["symbol"] for d in ctx.get("dropped_untradeable", [])}
    assert "LABUSDT" in dropped          # surfaced in the dropped list
    assert "BTCUSDT" not in dropped
    lab_drop = next(d for d in ctx["dropped_untradeable"] if d["symbol"] == "LABUSDT")
    assert lab_drop["atr_price_ratio"] >= 1.0   # carries the diagnostic ratio


def test_preflight_keeps_held_post_crush_position(tmp_path):
    # A HELD position on a post-crush name must NOT be dropped — RM still has to review it
    # (you are already in the trade). The filter exempts held symbols.
    from futures_fund.state import Position, save_positions
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    held = Position(symbol="LABUSDT", direction="long", qty=1.0, entry=1.0, stop=-1.0,
                    take_profits=[999.0], leverage=3.0, margin=33.3, liq_price=-10.0,
                    opened_cycle=0, opened_ts=dt.datetime(2026, 2, 1, tzinfo=UTC))
    save_positions(state_dir, [held])
    ex = _MultiExchange({"BTC/USDT:USDT": _uptrend(), "LAB/USDT:USDT": _post_crush()},
                        {"LABUSDT": "LAB/USDT:USDT"})
    ctx = preflight_step(ex, _settings(), state_dir, memory_dir,
                         now=dt.datetime(2026, 3, 1, tzinfo=UTC), cycle_no=2,
                         http_client=_HttpClient())
    briefed = {b["exchange_id"] for b in ctx["briefs"]}
    assert "LABUSDT" in briefed   # held position kept despite post-crush ATR
    assert "LABUSDT" not in {d["symbol"] for d in ctx.get("dropped_untradeable", [])}


def test_preflight_folds_pending_trigger_symbol_into_universe(tmp_path):
    # A PENDING-TRIGGER symbol must be BRIEFED each cycle even if it is NOT in the Watcher's picks
    # and not held — so the team can re-arm it with FRESH RR or retire it deliberately, rather than
    # let it silently lapse for want of data (cy236 NEAR). This is symmetric with the gate, which
    # already folds pending-trigger symbols (via _fold_raw_symbols) so it can EVALUATE them.
    from futures_fund.pending_orders import PendingOrder, save_pending_orders
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    trig = PendingOrder(symbol="NEARUSDT", direction="long", kind="limit_entry",
                        trigger_level=2.0, stop=1.9, take_profits=[2.3], atr=0.05,
                        created_cycle=1, expires_cycle=5)
    save_pending_orders(state_dir, [trig])
    # NEAR is neither in _settings() (symbols=[BTC]) nor held; the raw->unified map lets it fold.
    ex = _MultiExchange({"BTC/USDT:USDT": _uptrend(), "NEAR/USDT:USDT": _uptrend()},
                        {"NEARUSDT": "NEAR/USDT:USDT"})
    ctx = preflight_step(ex, _settings(), state_dir, memory_dir,
                         now=dt.datetime(2026, 3, 1, tzinfo=UTC), cycle_no=2,
                         http_client=_HttpClient())
    briefed = {b["exchange_id"] for b in ctx["briefs"]}
    assert "NEARUSDT" in briefed   # pending-trigger symbol briefed so the team can review/re-arm it


def test_preflight_keeps_pending_trigger_on_post_crush_name(tmp_path):
    # A pending trigger on a post-crush name must NOT be silently dropped from briefs: the team has
    # to SEE it to retire it deliberately (cancel_triggers), like a held post-crush position is
    # exempt. So a folded pending-trigger symbol is exempt from the post-crush candidate drop.
    from futures_fund.pending_orders import PendingOrder, save_pending_orders
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    trig = PendingOrder(symbol="LABUSDT", direction="short", kind="stop_entry",
                        trigger_level=1.0, stop=1.2, take_profits=[0.5], atr=2.0,
                        created_cycle=1, expires_cycle=5)
    save_pending_orders(state_dir, [trig])
    ex = _MultiExchange({"BTC/USDT:USDT": _uptrend(), "LAB/USDT:USDT": _post_crush()},
                        {"LABUSDT": "LAB/USDT:USDT"})
    ctx = preflight_step(ex, _settings(), state_dir, memory_dir,
                         now=dt.datetime(2026, 3, 1, tzinfo=UTC), cycle_no=2,
                         http_client=_HttpClient())
    briefed = {b["exchange_id"] for b in ctx["briefs"]}
    assert "LABUSDT" in briefed   # pending-trigger symbol kept for retire-review (post-crush ATR)
    assert "LABUSDT" not in {d["symbol"] for d in ctx.get("dropped_untradeable", [])}


class _UnionExchange(FakeExchange):
    """FakeExchange that can map a held raw id back to its unified symbol (like ccxt)."""
    def __init__(self, frames, raw_to_unified):
        super().__init__(frames)
        self._r2u = raw_to_unified

    def unified_for_raw(self, raw_id):
        return self._r2u.get(raw_id)

    def symbol_spec(self, symbol):
        from futures_fund.models import MmrBracket, SymbolSpec
        raw = {"BTC/USDT:USDT": "BTCUSDT", "ETH/USDT:USDT": "ETHUSDT"}.get(symbol, "BTCUSDT")
        return SymbolSpec(symbol=raw, tick_size=0.01, step_size=0.001, min_notional=5.0,
                          mmr_brackets=[MmrBracket(notional_floor=0, notional_cap=1_000_000,
                                                   mmr=0.004, maint_amount=0.0, max_leverage=125)])


def test_working_universe_folds_in_held_positions(tmp_path):
    # A position is held in ETH, but this cycle's configured universe is only BTC (the Watcher
    # rotated away). The held symbol MUST be folded in so it is briefed/priced, not stranded.
    from futures_fund.models import MmrBracket  # noqa: F401  (referenced by _UnionExchange)
    from futures_fund.state import Position, save_positions
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    held = Position(symbol="ETHUSDT", direction="long", qty=1.0, entry=100.0, stop=90.0,
                    take_profits=[120.0], leverage=3.0, margin=33.3, liq_price=70.0,
                    opened_cycle=0, opened_ts=dt.datetime(2026, 2, 1, tzinfo=UTC))
    save_positions(state_dir, [held])
    ex = _UnionExchange({"BTC/USDT:USDT": _uptrend(), "ETH/USDT:USDT": _uptrend()},
                        {"ETHUSDT": "ETH/USDT:USDT"})
    ctx = preflight_step(ex, _settings(), state_dir, memory_dir,  # _settings() -> symbols=[BTC]
                         now=dt.datetime(2026, 3, 1, tzinfo=UTC), cycle_no=2,
                         http_client=_HttpClient())
    briefed = {b["exchange_id"] for b in ctx["briefs"]}
    assert "BTCUSDT" in briefed and "ETHUSDT" in briefed  # held ETH folded into the universe


def _seed_holding(tmp_path):
    from futures_fund.state import Position, save_positions
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    held = Position(symbol="ETHUSDT", direction="long", qty=1.0, entry=100.0, stop=90.0,
                    take_profits=[130.0], leverage=3.0, margin=33.3, liq_price=70.0,
                    opened_cycle=1, opened_ts=dt.datetime(2026, 2, 1, tzinfo=UTC))
    save_positions(state_dir, [held])
    ex = _UnionExchange({"BTC/USDT:USDT": _uptrend(), "ETH/USDT:USDT": _uptrend()},
                        {"ETHUSDT": "ETH/USDT:USDT"})
    return state_dir, memory_dir, ex


def test_brief_carries_holding_card_for_open_position(tmp_path):
    from futures_fund.journal import append_decision
    from futures_fund.memory_layout import ensure_memory_layout
    from futures_fund.state import Position, save_positions
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    ensure_memory_layout(memory_dir)
    did = append_decision(memory_dir, {"ts": dt.datetime(2026, 2, 1, tzinfo=UTC), "cycle": 1,
                                       "symbol": "ETHUSDT", "direction": "long", "entry": 100.0,
                                       "stop": 90.0, "rationale": "L2 thesis",
                                       "falsifiable_prediction": "ETH > 999 in 6 cycles"})
    # TP set far above the uptrend so the audit does NOT close it -> it survives to be carded
    save_positions(state_dir, [Position(symbol="ETHUSDT", direction="long", qty=1.0, entry=100.0,
                                        stop=90.0, take_profits=[999.0], leverage=3.0, margin=33.3,
                                        liq_price=70.0, opened_cycle=1,
                                        opened_ts=dt.datetime(2026, 2, 1, tzinfo=UTC),
                                        decision_id=did)])
    ex = _UnionExchange({"BTC/USDT:USDT": _uptrend(), "ETH/USDT:USDT": _uptrend()},
                        {"ETHUSDT": "ETH/USDT:USDT"})
    ctx = preflight_step(ex, _settings(), state_dir, memory_dir,
                         now=dt.datetime(2026, 3, 1, tzinfo=UTC), cycle_no=2,
                         http_client=_HttpClient())
    eth = next(b for b in ctx["briefs"] if b["exchange_id"] == "ETHUSDT")
    assert "holding" in eth
    assert eth["holding"]["original_thesis"] == "L2 thesis"
    assert eth["holding"]["falsifiable_prediction"] == "ETH > 999 in 6 cycles"
    assert "r_progress" in eth["holding"] and "bars_held" in eth["holding"]
    btc = next(b for b in ctx["briefs"] if b["exchange_id"] == "BTCUSDT")
    assert "holding" not in btc  # not held -> no card


def test_holdings_review_keeps_holding_absent_from_new_opens(tmp_path):
    # The Watcher rotated away from ETH and no new ETH proposal exists, but the review said HOLD.
    # With an explicit review present, the holding must NOT be churned closed by absence.
    state_dir, memory_dir, ex = _seed_holding(tmp_path)
    report = gate_execute_step(ex, _settings(), state_dir, memory_dir,
                               now=dt.datetime(2026, 3, 1, tzinfo=UTC), cycle_no=2,
                               proposals=[], management=[{"symbol": "ETHUSDT", "action": "hold"}])
    assert report["closed"] == 0 and report["closed_by_review"] == 0
    assert {p.symbol for p in load_positions(state_dir)} == {"ETHUSDT"}


def test_holdings_review_closes_on_explicit_close(tmp_path):
    state_dir, memory_dir, ex = _seed_holding(tmp_path)
    report = gate_execute_step(ex, _settings(), state_dir, memory_dir,
                               now=dt.datetime(2026, 3, 1, tzinfo=UTC), cycle_no=2,
                               proposals=[], management=[{"symbol": "ETHUSDT", "action": "close"}])
    assert report["closed"] == 1 and report["closed_by_review"] == 1
    assert load_positions(state_dir) == []


def test_holdings_review_trails_stop_in_place(tmp_path):
    state_dir, memory_dir, ex = _seed_holding(tmp_path)
    report = gate_execute_step(
        ex, _settings(), state_dir, memory_dir, now=dt.datetime(2026, 3, 1, tzinfo=UTC),
        cycle_no=2, proposals=[],
        management=[{"symbol": "ETHUSDT", "action": "hold", "new_stop": 95.0}])
    assert report["trailed"] == 1
    pos = load_positions(state_dir)
    assert len(pos) == 1 and pos[0].stop == 95.0  # tightened, never loosened


def test_holdings_review_ignores_loosening_stop(tmp_path):
    state_dir, memory_dir, ex = _seed_holding(tmp_path)
    report = gate_execute_step(  # 80 is BELOW the existing 90 stop on a long -> looser, rejected
        ex, _settings(), state_dir, memory_dir, now=dt.datetime(2026, 3, 1, tzinfo=UTC),
        cycle_no=2, proposals=[],
        management=[{"symbol": "ETHUSDT", "action": "hold", "new_stop": 80.0}])
    assert report["trailed"] == 0
    assert load_positions(state_dir)[0].stop == 90.0


def test_holdings_review_trails_profit_lock_above_entry(tmp_path):
    # Winning long (entry 100, mark ~147): trail the stop ABOVE entry to 110 to LOCK PROFIT.
    state_dir, memory_dir, ex = _seed_holding(tmp_path)  # ETH long entry 100, stop 90
    report = gate_execute_step(
        ex, _settings(), state_dir, memory_dir, now=dt.datetime(2026, 3, 1, tzinfo=UTC),
        cycle_no=2, proposals=[],
        management=[{"symbol": "ETHUSDT", "action": "hold", "new_stop": 110.0}])
    assert report["trailed"] == 1
    assert load_positions(state_dir)[0].stop == 110.0  # profit-locking stop above entry


def test_holdings_review_rejects_trail_past_mark(tmp_path):
    # A stop beyond the current mark (~147) would instantly stop out — reject it gracefully.
    state_dir, memory_dir, ex = _seed_holding(tmp_path)  # ETH long, mark ~147
    report = gate_execute_step(
        ex, _settings(), state_dir, memory_dir, now=dt.datetime(2026, 3, 1, tzinfo=UTC),
        cycle_no=2, proposals=[],
        management=[{"symbol": "ETHUSDT", "action": "hold", "new_stop": 200.0}])
    assert report["trailed"] == 0 and load_positions(state_dir)[0].stop == 90.0


def test_holdings_review_reduce_banks_half_and_keeps_runner(tmp_path):
    from futures_fund.state import load_account
    state_dir, memory_dir, ex = _seed_holding(tmp_path)  # ETH long entry 100 qty 1.0, mark ~147
    default = _settings().account_size_usdt
    report = gate_execute_step(
        ex, _settings(), state_dir, memory_dir, now=dt.datetime(2026, 3, 1, tzinfo=UTC),
        cycle_no=2, proposals=[],
        management=[{"symbol": "ETHUSDT", "action": "reduce", "reduce_fraction": 0.5}])
    assert report["reduced"] == 1 and report["banked_pnl"] > 0 and report["closed"] == 0
    pos = load_positions(state_dir)
    assert len(pos) == 1 and pos[0].symbol == "ETHUSDT" and pos[0].qty == 0.5  # runner kept
    # only the reduce moved the wallet (no opens/closes) -> balance == default + banked
    assert load_account(state_dir, default).balance == default + report["banked_pnl"]


def test_holdings_review_reduce_works_for_short(tmp_path):
    from futures_fund.state import Position, save_positions
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    held = Position(symbol="ETHUSDT", direction="short", qty=2.0, entry=200.0, stop=210.0,
                    take_profits=[100.0], leverage=3.0, margin=133.0, liq_price=300.0,
                    opened_cycle=1, opened_ts=dt.datetime(2026, 2, 1, tzinfo=UTC))
    save_positions(state_dir, [held])
    ex = _UnionExchange({"BTC/USDT:USDT": _uptrend(), "ETH/USDT:USDT": _uptrend()},
                        {"ETHUSDT": "ETH/USDT:USDT"})  # ETH mark ~147 < entry 200 -> winning short
    report = gate_execute_step(
        ex, _settings(), state_dir, memory_dir, now=dt.datetime(2026, 3, 1, tzinfo=UTC),
        cycle_no=2, proposals=[],
        management=[{"symbol": "ETHUSDT", "action": "reduce", "reduce_fraction": 0.5}])
    assert report["reduced"] == 1 and report["banked_pnl"] > 0
    assert load_positions(state_dir)[0].qty == 1.0  # half of 2.0


def test_holdings_review_reduce_drops_bad_fraction(tmp_path):
    state_dir, memory_dir, ex = _seed_holding(tmp_path)
    report = gate_execute_step(
        ex, _settings(), state_dir, memory_dir, now=dt.datetime(2026, 3, 1, tzinfo=UTC),
        cycle_no=2, proposals=[],
        management=[{"symbol": "ETHUSDT", "action": "reduce", "reduce_fraction": 1.5}])
    assert report["reduced"] == 0 and report["reduce_dropped"] == 1
    assert load_positions(state_dir)[0].qty == 1.0  # untouched


def test_holdings_review_reduce_promotes_dust_to_full_close(tmp_path):
    state_dir, memory_dir, ex = _seed_holding(tmp_path)  # qty 1.0, mark ~147, min_notional 5.0
    report = gate_execute_step(  # remaining 0.01 * 147 = ~1.47 < 5 -> full close
        ex, _settings(), state_dir, memory_dir, now=dt.datetime(2026, 3, 1, tzinfo=UTC),
        cycle_no=2, proposals=[],
        management=[{"symbol": "ETHUSDT", "action": "reduce", "reduce_fraction": 0.99}])
    assert report["closed"] == 1 and report["reduced"] == 0
    assert load_positions(state_dir) == []


# ---- reduce v2: an OPTIONAL new_stop on a reduce banks AND trails the runner in one directive ----

def test_reduce_with_new_stop_banks_and_trails_runner(tmp_path):
    state_dir, memory_dir, ex = _seed_holding(tmp_path)  # ETH long entry 100, qty 1.0, stop 90, mark ~147
    report = gate_execute_step(
        ex, _settings(), state_dir, memory_dir, now=dt.datetime(2026, 3, 1, tzinfo=UTC),
        cycle_no=2, proposals=[],
        management=[{"symbol": "ETHUSDT", "action": "reduce", "reduce_fraction": 0.5,
                     "new_stop": 110.0}])  # 90 < 110 < mark 147 -> valid profit-lock trail
    assert report["reduced"] == 1 and report["trailed"] == 1  # banked AND trailed in one directive
    pos = load_positions(state_dir)
    assert len(pos) == 1 and pos[0].qty == 0.5 and pos[0].stop == 110.0  # runner trimmed + trailed


def test_reduce_rejects_loosening_new_stop_but_still_banks(tmp_path):
    state_dir, memory_dir, ex = _seed_holding(tmp_path)  # long, stop 90, mark ~147
    report = gate_execute_step(  # new_stop 80 is BELOW current 90 (looser for a long) -> rejected
        ex, _settings(), state_dir, memory_dir, now=dt.datetime(2026, 3, 1, tzinfo=UTC),
        cycle_no=2, proposals=[],
        management=[{"symbol": "ETHUSDT", "action": "reduce", "reduce_fraction": 0.5,
                     "new_stop": 80.0}])
    assert report["reduced"] == 1 and report["trailed"] == 0  # banked, trail rejected (not loosened)
    pos = load_positions(state_dir)
    assert pos[0].qty == 0.5 and pos[0].stop == 90.0  # runner keeps the original stop


def test_reduce_with_new_stop_trails_short_runner(tmp_path):
    from futures_fund.state import Position, save_positions
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    held = Position(symbol="ETHUSDT", direction="short", qty=2.0, entry=200.0, stop=210.0,
                    take_profits=[100.0], leverage=3.0, margin=133.0, liq_price=300.0,
                    opened_cycle=1, opened_ts=dt.datetime(2026, 2, 1, tzinfo=UTC))
    save_positions(state_dir, [held])
    ex = _UnionExchange({"BTC/USDT:USDT": _uptrend(), "ETH/USDT:USDT": _uptrend()},
                        {"ETHUSDT": "ETH/USDT:USDT"})  # ETH mark ~147 < entry 200 -> winning short
    report = gate_execute_step(  # short: mark(147) < new_stop(160) < cur_stop(210) -> valid
        ex, _settings(), state_dir, memory_dir, now=dt.datetime(2026, 3, 1, tzinfo=UTC),
        cycle_no=2, proposals=[],
        management=[{"symbol": "ETHUSDT", "action": "reduce", "reduce_fraction": 0.5,
                     "new_stop": 160.0}])
    assert report["reduced"] == 1 and report["trailed"] == 1
    pos = load_positions(state_dir)
    assert pos[0].qty == 1.0 and pos[0].stop == 160.0  # short runner trimmed + trailed (160 < 210)


def test_reduce_noop_dust_still_trails_with_new_stop(tmp_path):
    state_dir, memory_dir, ex = _seed_holding(tmp_path)  # long qty 1.0, stop 90, mark ~147
    report = gate_execute_step(  # fraction 0.0005 -> slice floors to 0 (dust); new_stop still trails
        ex, _settings(), state_dir, memory_dir, now=dt.datetime(2026, 3, 1, tzinfo=UTC),
        cycle_no=2, proposals=[],
        management=[{"symbol": "ETHUSDT", "action": "reduce", "reduce_fraction": 0.0005,
                     "new_stop": 110.0}])
    assert report["reduced"] == 0 and report["trailed"] == 1  # no bank (dust) but the trail applied
    pos = load_positions(state_dir)
    assert pos[0].qty == 1.0 and pos[0].stop == 110.0  # whole position kept, stop trailed


def _btc_long(entry=147.0):  # a valid new-open proposal on BTC (uptrend last close ~147)
    return {"symbol": "BTCUSDT", "direction": "long", "entry": entry, "stop": entry - 4.0,
            "take_profits": [entry + 8.0], "atr": 2.0, "confidence": 0.7, "rationale": "x"}


def test_halt_blocks_new_opens_but_still_closes(tmp_path):
    # Audit fix [2]: a halt tripped (e.g. by the monitor) must block NEW opens at the trade
    # boundary, but explicit holdings CLOSES must still run (a halt should de-risk, not freeze).
    from futures_fund.state import set_halt
    state_dir, memory_dir, ex = _seed_holding(tmp_path)  # held ETH long
    set_halt(state_dir, True, reason="monitor drawdown")
    report = gate_execute_step(ex, _settings(), state_dir, memory_dir,
                               now=dt.datetime(2026, 3, 1, tzinfo=UTC), cycle_no=2,
                               proposals=[_btc_long()],
                               management=[{"symbol": "ETHUSDT", "action": "close"}])
    assert report["halted"] is True
    assert report["opened"] == 0          # new BTC open blocked
    assert report["closed"] == 1          # ETH close still honored
    assert load_positions(state_dir) == []


def test_force_flatten_breaker_closes_all_holdings(tmp_path):
    # Audit fix [3]: a -12% month must FLATTEN the book regardless of the review's verdicts.
    from futures_fund.equity_log import record_equity
    state_dir, memory_dir, ex = _seed_holding(tmp_path)  # held ETH long
    now = dt.datetime(2026, 3, 1, tzinfo=UTC)
    record_equity(state_dir, now - dt.timedelta(days=31), 10_000.0, 0)
    record_equity(state_dir, now, 8_700.0, 1)            # -13% on the month -> force_flatten
    report = gate_execute_step(ex, _settings(), state_dir, memory_dir, now=now, cycle_no=2,
                               proposals=[], management=[{"symbol": "ETHUSDT", "action": "hold"}])
    assert "force_flatten" in report and report["closed"] == 1
    assert load_positions(state_dir) == []  # flattened despite the HOLD verdict


def test_review_never_flips_a_kept_holding(tmp_path):
    # Audit fix [1]: a HOLD + an opposite-direction proposal on the same symbol must NOT stack
    # into a simultaneous long+short. The kept holding is never re-opened/flipped.
    state_dir, memory_dir, ex = _seed_holding(tmp_path)  # held ETH long
    short_eth = {"symbol": "ETHUSDT", "direction": "short", "entry": 147.0, "stop": 151.0,
                 "take_profits": [140.0], "atr": 2.0, "confidence": 0.7, "rationale": "x"}
    report = gate_execute_step(ex, _settings(), state_dir, memory_dir,
                               now=dt.datetime(2026, 3, 1, tzinfo=UTC), cycle_no=2,
                               proposals=[short_eth],
                               management=[{"symbol": "ETHUSDT", "action": "hold"}])
    pos = load_positions(state_dir)
    assert len(pos) == 1 and pos[0].symbol == "ETHUSDT" and pos[0].direction == "long"
    assert report["opened"] == 0  # the counter-direction proposal was NOT opened


def test_explicit_close_then_same_direction_reopen(tmp_path):
    # Audit fix [5]: CLOSE a holding AND re-propose the same direction -> close+reopen (one net).
    state_dir, memory_dir, ex = _seed_holding(tmp_path)  # held ETH long entry 100
    reopen = {"symbol": "ETHUSDT", "direction": "long", "entry": 147.0, "stop": 143.0,
              "take_profits": [160.0], "atr": 2.0, "confidence": 0.7, "rationale": "x"}
    report = gate_execute_step(ex, _settings(), state_dir, memory_dir,
                               now=dt.datetime(2026, 3, 1, tzinfo=UTC), cycle_no=2,
                               proposals=[reopen],
                               management=[{"symbol": "ETHUSDT", "action": "close"}])
    assert report["closed"] == 1 and report["opened"] == 1 and report["closed_by_review"] == 1
    pos = load_positions(state_dir)
    assert len(pos) == 1 and pos[0].direction == "long" and pos[0].entry > 120  # the NEW long


def test_malformed_proposal_dropped_others_proceed(tmp_path):
    # Audit fix [4]: one malformed proposal must not abort the gate; good opens still execute.
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    ex = _UnionExchange({"BTC/USDT:USDT": _uptrend()}, {})
    bad = {"symbol": "BTCUSDT", "direction": "long", "entry": 100.0, "stop": 110.0,  # stop>entry
           "take_profits": [120.0], "atr": 2.0, "confidence": 0.7, "rationale": "inverted"}
    report = gate_execute_step(ex, _settings(), state_dir, memory_dir,
                               now=dt.datetime(2026, 3, 1, tzinfo=UTC), cycle_no=1,
                               proposals=[_btc_long(), bad], management=None)
    assert report["opened"] == 1 and report["dropped"] >= 1


def test_falsifiable_prediction_is_journaled_at_entry(tmp_path):
    # The RM's falsifiable_prediction must persist into the decision journal so the later
    # HOLD/CLOSE review can test it (previously it was dropped -> holding card showed null).
    from futures_fund.journal import read_open_decisions
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    ex = FakeExchange({"BTC/USDT:USDT": _uptrend()})
    pf = preflight_step(ex, _settings(), state_dir, memory_dir,
                        now=dt.datetime(2026, 3, 1, tzinfo=UTC), cycle_no=1,
                        http_client=_HttpClient())
    last = pf["briefs"][0]["last_close"]
    proposals = [{"symbol": "BTCUSDT", "direction": "long", "entry": last, "stop": last - 4.0,
                  "take_profits": [last + 8.0], "atr": 2.0, "confidence": 0.7, "rationale": "x",
                  "falsifiable_prediction": "BTC makes a higher high within 6 cycles"}]
    gate_execute_step(ex, _settings(), state_dir, memory_dir,
                      now=dt.datetime(2026, 3, 1, tzinfo=UTC), cycle_no=1, proposals=proposals)
    decs = read_open_decisions(memory_dir)
    assert len(decs) == 1
    assert decs[0]["falsifiable_prediction"] == "BTC makes a higher high within 6 cycles"


def test_empty_universe_stands_down(tmp_path):
    # Audit fix [11]: an empty universe (failed scan / degenerate picks) must stand down, not crash.
    from futures_fund.config import Settings
    ex = _UnionExchange({}, {})
    s = Settings(account_size_usdt=10_000.0, symbols=[], timeframe="4h")
    report = gate_execute_step(ex, s, tmp_path / "s", tmp_path / "m",
                               now=dt.datetime(2026, 3, 1, tzinfo=UTC), cycle_no=1,
                               proposals=[_btc_long()], management=None)
    assert report.get("stood_down") is True and report["opened"] == 0


def test_screen_step_returns_top_symbols(tmp_path):
    reports = [
        {"agent": "technical", "symbol": "BTCUSDT", "stance": "bullish", "confidence": 0.9},
        {"agent": "derivatives", "symbol": "BTCUSDT", "stance": "bullish", "confidence": 0.8},
        {"agent": "technical", "symbol": "ETHUSDT", "stance": "neutral", "confidence": 0.5},
    ]
    top = screen_step(reports, top_n=5)
    assert top == ["BTCUSDT"]


def test_gate_execute_step_opens_from_agent_proposals(tmp_path):
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    ex = FakeExchange({"BTC/USDT:USDT": _uptrend()})
    pf = preflight_step(ex, _settings(), state_dir, memory_dir,
                        now=dt.datetime(2026, 3, 1, tzinfo=UTC), cycle_no=1,
                        http_client=_HttpClient())
    last = pf["briefs"][0]["last_close"]
    proposals = [AgentProposal(symbol="BTCUSDT", direction="long", entry=last,
                               stop=last - 4.0, take_profits=[last + 8.0], atr=2.0,
                               confidence=0.7, rationale="bull thesis won the debate").model_dump()]
    report = gate_execute_step(ex, _settings(), state_dir, memory_dir,
                               now=dt.datetime(2026, 3, 1, tzinfo=UTC), cycle_no=1,
                               proposals=proposals)
    assert report["opened"] == 1
    pos = load_positions(state_dir)
    assert len(pos) == 1 and pos[0].decision_id is not None


def test_reflect_step_splits_winners_losers(tmp_path):
    from futures_fund.journal import append_decision, patch_outcome
    from futures_fund.memory_layout import ensure_memory_layout
    memory_dir = tmp_path / "m"
    ensure_memory_layout(memory_dir)
    did = append_decision(memory_dir, {"ts": dt.datetime(2026, 5, 1, tzinfo=UTC), "cycle": 1,
                                       "symbol": "BTCUSDT", "direction": "long",
                                       "entry": 100.0, "stop": 95.0})
    patch_outcome(memory_dir, did, {"realized_pnl": 42.0, "prediction_correct": True})
    payload = reflect_step(memory_dir)
    assert payload["n_closed"] == 1 and len(payload["winners"]) == 1


def test_preflight_brief_includes_exchange_id(tmp_path):
    ex = FakeExchange({"BTC/USDT:USDT": _uptrend()})
    ctx = preflight_step(ex, _settings(), tmp_path / "s", tmp_path / "m",
                         now=dt.datetime(2026, 3, 1, tzinfo=UTC), cycle_no=1,
                         http_client=_HttpClient())
    assert ctx["briefs"][0]["exchange_id"] == "BTCUSDT"


def test_gate_execute_normalizes_unified_symbol(tmp_path):
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    ex = FakeExchange({"BTC/USDT:USDT": _uptrend()})
    pf = preflight_step(ex, _settings(), state_dir, memory_dir,
                        now=dt.datetime(2026, 3, 1, tzinfo=UTC), cycle_no=1,
                        http_client=_HttpClient())
    last = pf["briefs"][0]["last_close"]
    # proposal emitted with the UNIFIED symbol must still execute (normalized to raw)
    proposals = [{"symbol": "BTC/USDT:USDT", "direction": "long", "entry": last,
                  "stop": last - 4.0, "take_profits": [last + 8.0], "atr": 2.0,
                  "confidence": 0.7, "rationale": "x"}]
    report = gate_execute_step(ex, _settings(), state_dir, memory_dir,
                               now=dt.datetime(2026, 3, 1, tzinfo=UTC), cycle_no=1,
                               proposals=proposals)
    assert report["opened"] == 1 and report["dropped"] == 0


def test_preflight_attaches_market_context(tmp_path):
    ex = FakeExchange({"BTC/USDT:USDT": _uptrend()})
    ctx = preflight_step(ex, _settings(), tmp_path / "s", tmp_path / "m",
                         now=datetime(2026, 3, 1, tzinfo=UTC), cycle_no=1,
                         http_client=_HttpClient())
    mc = ctx["market_context"]
    assert mc["fear_greed"]["value"] == 30
    assert isinstance(mc["news"], list)
    assert "warnings" in mc
    # the brief now carries derivatives positioning
    assert "long_short_ratio" in ctx["briefs"][0]


# ---- Fix A: the regime is classified over the STABLE canonical majors panel, NOT just the
# Watcher's shortlist. A thin shortlist (e.g. 2 majors) must NOT collapse the label to 'mixed' on a
# deeply risk_off tape (the cycle-29 quorum artifact). preflight builds regime_panel_only briefs for
# the canonical majors absent from the universe so quorum/breadth read the full panel. ----

def _downtrend(n=60):
    rng = np.random.default_rng(11)
    close = 100.0 - 0.8 * np.arange(n) + rng.normal(0, 0.05, n)
    return pd.DataFrame({
        "timestamp": pd.date_range("2026-01-01", periods=n, freq="4h", tz="UTC"),
        "open": close, "high": close + 0.2, "low": close - 0.2, "close": close, "volume": 1.0,
    })


_MAJORS_UNIFIED = {"BTC/USDT:USDT": "BTCUSDT", "ETH/USDT:USDT": "ETHUSDT",
                   "BNB/USDT:USDT": "BNBUSDT", "SOL/USDT:USDT": "SOLUSDT",
                   "XRP/USDT:USDT": "XRPUSDT"}


def test_regime_classified_over_full_majors_panel(tmp_path):
    # Shortlist is ONLY BTC, but all five majors are deeply down. The regime must pull the missing
    # four majors into the panel so quorum holds (>=3 + BTC) and the tape is labeled risk_off — not
    # 'mixed' for lack of majors in the shortlist.
    frames = {u: _downtrend() for u in _MAJORS_UNIFIED}
    r2u = {raw: u for u, raw in _MAJORS_UNIFIED.items()}
    ex = _UnionExchange(frames, r2u)  # unified_for_raw maps all five
    ctx = preflight_step(ex, _settings(), tmp_path / "s", tmp_path / "m",  # _settings symbols=[BTC]
                         now=dt.datetime(2026, 3, 1, tzinfo=UTC), cycle_no=1,
                         http_client=_HttpClient())
    drv = ctx["regime_state"]["drivers"]
    assert drv["quorum_met"] is True
    assert set(drv["majors_present"]) == set(_MAJORS_UNIFIED.values())  # all five seen
    assert drv["deterministic_regime"] == "risk_off"  # deep down-tape labeled, not 'mixed'
    briefed = {b["exchange_id"] for b in ctx["briefs"]}
    assert briefed == set(_MAJORS_UNIFIED.values())
    # only the four majors NOT in the shortlist are tagged regime-panel-only (BTC is a real brief)
    panel = {b["exchange_id"] for b in ctx["briefs"] if b.get("regime_panel_only")}
    assert panel == {"ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT"}
    assert not next(b for b in ctx["briefs"] if b["exchange_id"] == "BTCUSDT").get(
        "regime_panel_only")


def test_regime_panel_augmentation_is_failsafe_without_unified_mapping(tmp_path):
    # Base FakeExchange has no unified_for_raw and can price only BTC: the panel augmentation must
    # silently no-op (regime sees only the shortlist, exactly as before) and NEVER crash preflight.
    ex = FakeExchange({"BTC/USDT:USDT": _downtrend()})
    ctx = preflight_step(ex, _settings(), tmp_path / "s", tmp_path / "m",
                         now=dt.datetime(2026, 3, 1, tzinfo=UTC), cycle_no=1,
                         http_client=_HttpClient())
    briefed = {b["exchange_id"] for b in ctx["briefs"]}
    assert briefed == {"BTCUSDT"}  # no panel majors added, no crash
    assert not any(b.get("regime_panel_only") for b in ctx["briefs"])


# ---- Fix B: an advisory (non-blocking) warning when a HOLD/reduce-v2 new_stop is trailed to within
# ~0.6 ATR of the mark on a high-ATR name (the cycle-28 noise-stop / wick-out lesson). ----

def test_holdings_trail_into_noise_band_warns(tmp_path):
    state_dir, memory_dir, ex = _seed_holding(tmp_path)  # ETH long, stop 90, mark ~147, ATR ~1
    mark = float(_uptrend()["close"].iloc[-1])
    report = gate_execute_step(
        ex, _settings(), state_dir, memory_dir, now=dt.datetime(2026, 3, 1, tzinfo=UTC),
        cycle_no=2, proposals=[],
        management=[{"symbol": "ETHUSDT", "action": "hold", "new_stop": mark - 0.05}])
    assert report["trailed"] == 1  # the trail STILL applies — the warning never blocks
    assert load_positions(state_dir)[0].stop == mark - 0.05
    warns = report.get("warnings", [])
    assert any("noise band" in w and "ETHUSDT" in w for w in warns)


def test_holdings_trail_noise_band_discriminates_by_distance(tmp_path):
    # Non-vacuous: prove the warning is DISTANCE-driven, not a dead feature. On identical setups
    # (ETH long, mark ~147, ATR ~1, band ~0.6), an inside-band trail warns and an outside-band trail
    # is silent — and BOTH trails still apply (advisory never blocks). [Seed 7 -> mark~147, ATR~1.0]
    mark = float(_uptrend()["close"].iloc[-1])
    s_in, m_in, ex_in = _seed_holding(tmp_path / "inside")
    r_in = gate_execute_step(ex_in, _settings(), s_in, m_in,
                             now=dt.datetime(2026, 3, 1, tzinfo=UTC), cycle_no=2, proposals=[],
                             management=[{"symbol": "ETHUSDT", "action": "hold",
                                          "new_stop": mark - 0.05}])
    s_out, m_out, ex_out = _seed_holding(tmp_path / "outside")
    r_out = gate_execute_step(ex_out, _settings(), s_out, m_out,  # 110 is ~37 below mark
                              now=dt.datetime(2026, 3, 1, tzinfo=UTC), cycle_no=2, proposals=[],
                              management=[{"symbol": "ETHUSDT", "action": "hold",
                                           "new_stop": 110.0}])
    assert any("noise band" in w for w in r_in.get("warnings", []))        # inside -> warns
    assert not any("noise band" in w for w in r_out.get("warnings", []))   # outside -> silent
    assert r_in["trailed"] == 1 and r_out["trailed"] == 1                  # both trails applied


def test_short_position_trail_into_noise_band_warns(tmp_path):
    # Market-neutral symmetry: the noise-band guard must fire on a SHORT trail too (mark < new_stop
    # < cur_stop). ETH short, mark ~147; trail the stop to just above mark -> inside the band.
    from futures_fund.state import Position, save_positions
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    mark = float(_uptrend()["close"].iloc[-1])  # ETH mark ~147
    held = Position(symbol="ETHUSDT", direction="short", qty=2.0, entry=200.0, stop=210.0,
                    take_profits=[100.0], leverage=3.0, margin=133.0, liq_price=300.0,
                    opened_cycle=1, opened_ts=dt.datetime(2026, 2, 1, tzinfo=UTC))
    save_positions(state_dir, [held])
    ex = _UnionExchange({"BTC/USDT:USDT": _uptrend(), "ETH/USDT:USDT": _uptrend()},
                        {"ETHUSDT": "ETH/USDT:USDT"})
    report = gate_execute_step(  # short: mark(147) < new_stop(mark+0.05) < cur_stop(210) -> valid
        ex, _settings(), state_dir, memory_dir, now=dt.datetime(2026, 3, 1, tzinfo=UTC),
        cycle_no=2, proposals=[],
        management=[{"symbol": "ETHUSDT", "action": "hold", "new_stop": mark + 0.05}])
    assert report["trailed"] == 1
    assert any("noise band" in w and "ETHUSDT" in w for w in report.get("warnings", []))


def test_regime_panel_no_double_count(tmp_path):
    # BTC and ETH are BOTH in the shortlist AND canonical majors: each must appear exactly once and
    # NOT be tagged regime_panel_only; only the 3 missing majors are added as panel-only.
    frames = {u: _downtrend() for u in _MAJORS_UNIFIED}
    r2u = {raw: u for u, raw in _MAJORS_UNIFIED.items()}
    ex = _UnionExchange(frames, r2u)
    settings = Settings(account_size_usdt=10_000.0,
                        symbols=["BTC/USDT:USDT", "ETH/USDT:USDT"], timeframe="4h")
    ctx = preflight_step(ex, settings, tmp_path / "s", tmp_path / "m",
                         now=dt.datetime(2026, 3, 1, tzinfo=UTC), cycle_no=1,
                         http_client=_HttpClient())
    ids = [b["exchange_id"] for b in ctx["briefs"]]
    assert ids.count("BTCUSDT") == 1 and ids.count("ETHUSDT") == 1  # shortlist majors not doubled
    panel = {b["exchange_id"] for b in ctx["briefs"] if b.get("regime_panel_only")}
    assert panel == {"BNBUSDT", "SOLUSDT", "XRPUSDT"}  # only the 3 absent majors augmented


def test_reclassify_benefits_from_augmented_panel(tmp_path):
    # The cycle-29 bug was IN reclassify (Phase 4.6 saw only 2 majors). Since the panel is persisted
    # into context['briefs'], reclassify re-derives quorum/label over the full 5-major panel too.
    frames = {u: _downtrend() for u in _MAJORS_UNIFIED}
    r2u = {raw: u for u, raw in _MAJORS_UNIFIED.items()}
    ex = _UnionExchange(frames, r2u)
    state_dir = tmp_path / "s"
    ctx = preflight_step(ex, _settings(), state_dir, tmp_path / "m",  # shortlist = [BTC] only
                         now=dt.datetime(2026, 3, 1, tzinfo=UTC), cycle_no=1,
                         http_client=_HttpClient())
    assert ctx["regime_state"]["drivers"]["quorum_met"] is True  # preflight already full-panel
    from futures_fund.orchestration import reclassify_step
    news = [{"agent": "news", "symbol": "BTCUSDT", "signals": {"risk_off_flag": 0}}]
    rs = reclassify_step(state_dir, ctx, news, now=dt.datetime(2026, 3, 1, tzinfo=UTC))
    drv = rs["drivers"]
    assert drv["quorum_met"] is True
    assert set(drv["majors_present"]) == set(_MAJORS_UNIFIED.values())  # reclassify saw all 5
    assert drv["deterministic_regime"] == "risk_off"  # not 'mixed' for lack of majors


def test_reduce_trail_into_noise_band_warns(tmp_path):
    state_dir, memory_dir, ex = _seed_holding(tmp_path)  # ETH long qty 1.0, stop 90, mark ~147
    mark = float(_uptrend()["close"].iloc[-1])
    report = gate_execute_step(
        ex, _settings(), state_dir, memory_dir, now=dt.datetime(2026, 3, 1, tzinfo=UTC),
        cycle_no=2, proposals=[],
        management=[{"symbol": "ETHUSDT", "action": "reduce", "reduce_fraction": 0.5,
                     "new_stop": mark - 0.05}])  # banks half AND trails the runner into the band
    assert report["reduced"] == 1 and report["trailed"] == 1
    assert any("noise band" in w and "ETHUSDT" in w for w in report.get("warnings", []))

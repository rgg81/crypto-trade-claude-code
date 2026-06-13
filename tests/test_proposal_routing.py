"""A resting conditional order (kind stop_entry/limit_entry) misrouted into the MARKET-intent
`proposals` channel must be RE-ROUTED to `triggers`, never silently mangled (cy70 regression: a
with-regime stop_entry in `proposals` opened as a no-op market entry, a counter-regime one was
dropped). `split_misrouted_resting` is pure; the gate surfaces the reroute LOUD."""
from futures_fund.orchestration import normalize_trigger_level, split_misrouted_resting


def test_normalize_trigger_level_keeps_existing():
    d = {"symbol": "X", "trigger_level": 5.0}
    assert normalize_trigger_level(d) == d


def test_normalize_trigger_level_maps_trigger_price():
    # cy84 bug: the Trader emitted a SOL stop_entry with `trigger_price` (not `trigger_level`),
    # so PendingOrder ingestion raised and the trigger was SILENTLY dropped (SOL never armed).
    d = {"symbol": "SOLUSDT", "direction": "long", "kind": "stop_entry", "trigger_price": 68.85}
    out = normalize_trigger_level(d)
    assert out["trigger_level"] == 68.85
    assert d.get("trigger_level") is None  # input not mutated (shallow copy)


def test_normalize_trigger_level_maps_entry_when_no_trigger():
    d = {"symbol": "X", "entry": 7.0}
    assert normalize_trigger_level(d)["trigger_level"] == 7.0


def test_normalize_trigger_level_priority_and_passthrough():
    # explicit trigger_level wins over the synonyms; non-dict / no-key inputs pass through unchanged
    both = {"trigger_level": 1.0, "trigger_price": 2.0}
    assert normalize_trigger_level(both)["trigger_level"] == 1.0
    assert normalize_trigger_level({"symbol": "X"}) == {"symbol": "X"}
    assert normalize_trigger_level(None) is None


def test_split_misrouted_resting_normalizes_trigger_price():
    p = {"symbol": "X", "direction": "long", "kind": "stop_entry",
         "trigger_price": 7.0, "stop": 6.0, "take_profits": [9.0]}
    _market, _triggers, rerouted = split_misrouted_resting([p], [])
    assert rerouted[0]["trigger_level"] == 7.0


def test_market_proposals_pass_through_untouched():
    props = [{"symbol": "BTCUSDT", "direction": "long", "entry": 100.0}]
    market, triggers, rerouted = split_misrouted_resting(props, [])
    assert market == props and triggers == [] and rerouted == []


def test_stop_entry_in_proposals_is_rerouted_to_triggers():
    p = {"symbol": "X", "direction": "short", "kind": "stop_entry",
         "trigger_level": 9.0, "stop": 10.0, "take_profits": [5.0]}
    market, triggers, rerouted = split_misrouted_resting([p], [])
    assert market == [] and len(triggers) == 1 and len(rerouted) == 1
    assert triggers[0] is rerouted[0]


def test_limit_entry_also_rerouted_and_entry_maps_to_trigger_level():
    # a proposal-shaped dict carries `entry`; a trigger needs `trigger_level` -> copy it across so
    # PendingOrder ingestion downstream succeeds.
    p = {"symbol": "X", "direction": "long", "kind": "limit_entry",
         "entry": 7.0, "stop": 6.0, "take_profits": [9.0]}
    market, triggers, rerouted = split_misrouted_resting([p], [])
    assert market == [] and rerouted[0]["trigger_level"] == 7.0


def test_existing_trigger_level_not_clobbered_by_entry():
    p = {"symbol": "X", "direction": "short", "kind": "stop_entry",
         "trigger_level": 9.0, "entry": 99.0, "stop": 10.0, "take_profits": [5.0]}
    _, _, rerouted = split_misrouted_resting([p], [])
    assert rerouted[0]["trigger_level"] == 9.0   # explicit trigger_level wins over entry


def test_preserves_existing_triggers_then_appends_rerouted():
    existing = [{"symbol": "T", "kind": "stop_entry"}]
    props = [{"symbol": "M", "direction": "long", "entry": 1.0},
             {"symbol": "R", "direction": "short", "kind": "stop_entry",
              "trigger_level": 2.0, "stop": 3.0, "take_profits": [1.0]}]
    market, triggers, _ = split_misrouted_resting(props, existing)
    assert [p["symbol"] for p in market] == ["M"]
    assert [x["symbol"] for x in triggers] == ["T", "R"]   # existing first, then rerouted


def test_none_inputs_are_safe():
    assert split_misrouted_resting(None, None) == ([], [], [])


def test_non_dict_proposal_treated_as_market():
    market, _, rerouted = split_misrouted_resting(["weird"], [])
    assert market == ["weird"] and rerouted == []


def test_reroute_does_not_mutate_input_dict():
    p = {"symbol": "X", "direction": "long", "kind": "limit_entry", "entry": 7.0}
    split_misrouted_resting([p], [])
    assert "trigger_level" not in p   # original untouched; the copy carries the mapped key

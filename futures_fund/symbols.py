"""Shape-agnostic symbol key (cy78 backlog).

The desk juggles two symbol shapes: the RAW exchange id (`XMRUSDT`, used by proposals, screen.json,
and the gate's `ground_truth`) and the UNIFIED ccxt id (`XMR/USDT:USDT`, the brief/context key). A
lookup that matches on raw-OR-unified must reduce both to one key. `sym_key` does that — used by the
price-card filter AND the anti-hallucination proposal audit so neither fails-OPEN/empty on a shape
mismatch (the cy78 audit could fail-open on a unified-symbol proposal; the cards returned nothing
for raw screen symbols). Pure, total, never raises."""
from __future__ import annotations


def sym_key(s) -> str:
    """Reduce a symbol of either shape to one comparable key: 'XMR/USDT:USDT' and 'XMRUSDT' both ->
    'XMRUSDT'. Drops the ccxt settlement suffix (everything from ':') and the '/' separator, then
    upper-cases. Distinct bases never collide (USD-M perps are all *-USDT)."""
    return str(s).split(":")[0].replace("/", "").upper()

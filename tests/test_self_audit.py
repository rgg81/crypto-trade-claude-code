"""The standing self-audit panel (Pillar 4) must PASS on healthy code — every critical
safety/symmetry/pacing invariant holds."""
from futures_fund.self_audit import run_self_audit


def test_self_audit_all_invariants_pass():
    res = run_self_audit()
    failed = [c["name"] for c in res["checks"] if not c["ok"]]
    assert res["ok"], f"self-audit FAILED: {failed} -> {res['checks']}"


def test_self_audit_covers_the_critical_invariants():
    names = {c["name"] for c in run_self_audit()["checks"]}
    for must in ("pacing.anti_martingale", "gate.rr_floor>=2", "audit.drops_fabrication",
                 "audit.long_short_symmetric", "playbook.regime_routing"):
        assert must in names

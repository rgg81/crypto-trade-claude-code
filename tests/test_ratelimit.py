from futures_fund.ratelimit import WeightLimiter


def test_allows_within_capacity():
    rl = WeightLimiter(capacity=100, window_seconds=60)
    assert rl.allow(40, now=0.0) is True
    assert rl.allow(50, now=1.0) is True   # 90 used


def test_blocks_when_over_capacity():
    rl = WeightLimiter(capacity=100, window_seconds=60)
    rl.allow(80, now=0.0)
    assert rl.allow(30, now=1.0) is False  # 110 > 100


def test_window_expiry_frees_capacity():
    rl = WeightLimiter(capacity=100, window_seconds=60)
    rl.allow(80, now=0.0)
    assert rl.allow(80, now=61.0) is True  # the first 80 aged out of the 60s window


def test_used_weight_reports_current_window():
    rl = WeightLimiter(capacity=100, window_seconds=60)
    rl.allow(40, now=0.0)
    rl.allow(20, now=10.0)
    assert rl.used(now=10.0) == 60
    # t=0 event (40) aged out of the 60s window; t=10 event (20) survives
    assert rl.used(now=65.0) == 20

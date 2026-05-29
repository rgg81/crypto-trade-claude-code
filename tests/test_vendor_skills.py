import importlib

import pytest


@pytest.mark.parametrize(
    "module, attr",
    [
        ("futures_fund.vendor.regime_detection", "classify_regime"),
        ("futures_fund.vendor.regime_detection", "compute_atr"),
        ("futures_fund.vendor.feature_engineering", "build_all_features"),
        ("futures_fund.vendor.walk_forward", "WalkForwardValidator"),
        ("futures_fund.vendor.overfit_detector", "deflated_sharpe_ratio"),
    ],
)
def test_vendored_module_imports_and_exposes_api(module, attr):
    m = importlib.import_module(module)
    assert hasattr(m, attr), f"{module} is missing {attr}"


def test_deflated_sharpe_ratio_runs_and_returns_probability():
    from futures_fund.vendor.overfit_detector import deflated_sharpe_ratio

    # NOTE: this function returns a DSRResult dataclass (fields include dsr_pvalue: float,
    # is_significant: bool), NOT a bare float.
    result = deflated_sharpe_ratio(observed_sr=2.0, num_trials=10, backtest_length=500)
    assert 0.0 <= result.dsr_pvalue <= 1.0  # confirms scipy imports and the computation runs
    assert isinstance(result.is_significant, bool)

from futures_fund.config import Settings, load_settings


def test_defaults_when_no_file(tmp_path):
    s = load_settings(tmp_path / "missing.yaml")
    assert s.account_size_usdt == 10_000.0
    assert s.timeframe == "4h"
    assert s.symbol_count == 10
    assert s.exchange.testnet is True
    assert s.data.fred_series == ["DTWEXBGS", "DGS10", "FEDFUNDS", "CPIAUCSL"]


def test_yaml_overrides(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("account_size_usdt: 25000\nsymbol_count: 5\nexchange:\n  testnet: false\n")
    s = load_settings(p)
    assert s.account_size_usdt == 25000.0
    assert s.symbol_count == 5
    assert s.exchange.testnet is False


def test_secrets_read_from_env(monkeypatch):
    monkeypatch.setenv("BINANCE_KEY", "abc")
    monkeypatch.setenv("BINANCE_SECRET", "xyz")
    s = Settings()
    assert s.exchange.api_key == "abc"
    assert s.exchange.api_secret == "xyz"


def test_missing_secret_is_none(monkeypatch):
    monkeypatch.delenv("CRYPTOPANIC_TOKEN", raising=False)
    s = Settings()
    assert s.data.cryptopanic_token is None

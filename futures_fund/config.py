from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class ExchangeSettings(BaseModel):
    testnet: bool = True
    key_env: str = "BINANCE_KEY"
    secret_env: str = "BINANCE_SECRET"

    @property
    def api_key(self) -> str | None:
        return os.environ.get(self.key_env)

    @property
    def api_secret(self) -> str | None:
        return os.environ.get(self.secret_env)


class DataSettings(BaseModel):
    cryptopanic_token_env: str = "CRYPTOPANIC_TOKEN"
    fred_key_env: str = "FRED_API_KEY"
    fred_series: list[str] = Field(
        default_factory=lambda: ["DTWEXBGS", "DGS10", "FEDFUNDS", "CPIAUCSL"]
    )
    archive_dir: str = "state/archive"

    @property
    def cryptopanic_token(self) -> str | None:
        return os.environ.get(self.cryptopanic_token_env)

    @property
    def fred_api_key(self) -> str | None:
        return os.environ.get(self.fred_key_env)


class Settings(BaseModel):
    account_size_usdt: float = 10_000.0
    timeframe: str = "4h"
    symbol_count: int = 10
    deep_model: str = "opus"
    quick_model: str = "haiku"
    verdict_horizon_weeks: int = 8
    exchange: ExchangeSettings = Field(default_factory=ExchangeSettings)
    data: DataSettings = Field(default_factory=DataSettings)


def load_settings(path: str | Path = "config.yaml") -> Settings:
    """Load non-secret config from YAML (defaults if file absent). Secrets come from env."""
    p = Path(path)
    raw = yaml.safe_load(p.read_text()) if p.exists() else {}
    return Settings(**(raw or {}))

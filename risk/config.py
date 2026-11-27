"""Application settings loaded from environment variables.

Validation happens at import time; the process exits if any required field
is missing or invalid, so downstream code can assume settings are correct.
"""

from __future__ import annotations

import sys
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # GraphOps
    graphops_api_key: str = Field(default="", description="GraphOps API key")
    graphops_rpc_url: str = Field(
        default="https://mainnet.gateway.tenderly.co",
        description="Ethereum mainnet JSON-RPC endpoint",
    )
    graphops_substreams_endpoint: str = Field(
        default="mainnet.eth.streamingfast.io:443",
        description="Substreams gRPC endpoint",
    )

    # Database
    database_url: str = Field(..., description="PostgreSQL DSN")

    # Indexer behaviour
    backfill_days: int = Field(default=90, ge=1, le=365)
    snapshot_interval_live: int = Field(default=3600, ge=60)
    snapshot_interval_history: int = Field(default=86400, ge=3600)
    max_rpc_calls_per_run: int = Field(default=50000, ge=100)
    confirmations: int = Field(default=12, ge=1)

    # Alerts
    telegram_bot_token: str = Field(default="")
    telegram_chat_id: str = Field(default="")

    # API server
    api_host: str = Field(default="0.0.0.0")
    api_port: int = Field(default=8000, ge=1, le=65535)

    # Logging
    log_level: str = Field(default="INFO")

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        valid = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        upper = v.upper()
        if upper not in valid:
            raise ValueError(f"log_level must be one of {valid}")
        return upper

    @model_validator(mode="after")
    def warn_missing_optional(self) -> Settings:
        if not self.graphops_api_key:
            print(
                "WARNING: GRAPHOPS_API_KEY is not set. "
                "Substreams and authenticated RPC calls will fail.",
                file=sys.stderr,
            )
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def load_yaml(path: Path) -> Any:
    with open(path) as fh:
        return yaml.safe_load(fh)


def load_assets_config() -> dict[str, Any]:
    return load_yaml(ROOT / "config" / "assets.yaml")  # type: ignore[return-value]


def load_signals_config() -> dict[str, Any]:
    return load_yaml(ROOT / "config" / "signals.yaml")  # type: ignore[return-value]


def load_events_config() -> dict[str, Any]:
    return load_yaml(ROOT / "config" / "events.yaml")  # type: ignore[return-value]

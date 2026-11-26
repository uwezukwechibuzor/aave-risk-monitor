"""Tests for settings validation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError


def test_settings_missing_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """Settings must fail fast if DATABASE_URL is missing."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    # Clear lru_cache so Settings is re-instantiated
    from risk.config import get_settings

    get_settings.cache_clear()

    with pytest.raises((ValidationError, Exception)):
        from risk.config import Settings

        Settings()  # Should raise because DATABASE_URL has no default


def test_settings_valid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://risk:risk@localhost:5432/risk")
    monkeypatch.setenv("GRAPHOPS_API_KEY", "test-key")

    from risk.config import get_settings

    get_settings.cache_clear()
    from risk.config import Settings

    s = Settings()
    assert s.database_url == "postgresql://risk:risk@localhost:5432/risk"
    assert s.backfill_days == 90
    assert s.confirmations == 12


def test_settings_invalid_log_level(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://risk:risk@localhost:5432/risk")
    monkeypatch.setenv("LOG_LEVEL", "VERBOSE")

    from risk.config import Settings

    with pytest.raises(ValidationError):
        Settings()


def test_settings_backfill_days_min(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://risk:risk@localhost:5432/risk")
    monkeypatch.setenv("BACKFILL_DAYS", "0")

    from risk.config import Settings

    with pytest.raises(ValidationError):
        Settings()

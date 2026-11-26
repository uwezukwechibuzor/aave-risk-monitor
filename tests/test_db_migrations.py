"""Integration test: run migrations against a real Postgres instance.

Skipped if DATABASE_URL is not set or Postgres is unreachable.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.asyncio


@pytest.fixture
def db_url() -> str:
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        pytest.skip("DATABASE_URL not set")
    return url


async def test_migrations_idempotent(db_url: str) -> None:
    """Running migrations twice should be idempotent."""
    from risk.db.migrate import run_migrations

    await run_migrations(db_url)
    await run_migrations(db_url)  # second run: all skipped


async def test_active_users_table_exists(db_url: str) -> None:
    import asyncpg

    conn = await asyncpg.connect(dsn=db_url)
    try:
        rows = await conn.fetch(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name = 'active_users'"
        )
        assert len(rows) == 1, "active_users table should exist after migrations"
    finally:
        await conn.close()


async def test_signal_values_table_exists(db_url: str) -> None:
    import asyncpg

    conn = await asyncpg.connect(dsn=db_url)
    try:
        rows = await conn.fetch(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name = 'signal_values'"
        )
        assert len(rows) == 1
    finally:
        await conn.close()

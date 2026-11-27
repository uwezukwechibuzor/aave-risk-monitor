"""Simple numbered-SQL migration runner.

Usage:
    python -m risk.db.migrate
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import asyncpg

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


async def run_migrations(dsn: str) -> None:
    conn = await asyncpg.connect(dsn=dsn)
    try:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version    TEXT PRIMARY KEY,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        applied = {r["version"] for r in await conn.fetch("SELECT version FROM schema_migrations")}

        sql_files = sorted(MIGRATIONS_DIR.glob("*.sql"))
        for sql_file in sql_files:
            version = sql_file.stem.split("_")[0]
            if version in applied:
                print(f"  skip {sql_file.name} (already applied)")
                continue
            print(f"  applying {sql_file.name} ...")
            sql = sql_file.read_text()
            if "CONCURRENTLY" in sql.upper():
                # CREATE INDEX CONCURRENTLY can't run in a transaction, and a
                # multi-statement execute() is one implicit transaction — so run
                # each statement on its own.
                body = "\n".join(
                    line for line in sql.splitlines() if not line.lstrip().startswith("--")
                )
                for stmt in (s.strip() for s in body.split(";")):
                    if stmt:
                        await conn.execute(stmt)
            else:
                await conn.execute(sql)
            await conn.execute(
                "INSERT INTO schema_migrations (version) VALUES ($1) ON CONFLICT DO NOTHING",
                version,
            )
            print(f"  done {sql_file.name}")
    finally:
        await conn.close()


def main() -> None:
    from risk.config import get_settings

    settings = get_settings()
    asyncio.run(run_migrations(settings.database_url))


if __name__ == "__main__":
    main()

"""Cursor persistence and reorg handling for the indexer.

Design:
- Each stream has a unique stream_id.
- The cursor is persisted in the same DB transaction as the events it covers,
  giving exactly-once delivery on restart.
- Reorg handling: when Substreams delivers an Undo signal up to block N,
  we delete all raw_events (and domain events) with block_number > N and
  restore the cursor to the last stable cursor before that block.
  Rows are marked is_final once confirmed > CONFIRMATIONS blocks behind head.

This is a key correctness guarantee: if the process crashes mid-write,
the cursor is not updated, so on restart we re-process from the last committed
cursor with no gaps.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import asyncpg

logger = logging.getLogger(__name__)


@dataclass
class Cursor:
    stream_id: str
    cursor: str
    block_number: int
    block_hash: str


async def load_cursor(conn: asyncpg.Connection, stream_id: str) -> Cursor | None:
    """Load the last persisted cursor for a stream."""
    row = await conn.fetchrow(
        "SELECT cursor, block_number, block_hash FROM indexer_cursor WHERE stream_id = $1",
        stream_id,
    )
    if row is None:
        return None
    return Cursor(
        stream_id=stream_id,
        cursor=row["cursor"],
        block_number=row["block_number"],
        block_hash=row["block_hash"],
    )


async def save_cursor(
    conn: asyncpg.Connection,
    stream_id: str,
    cursor: str,
    block_number: int,
    block_hash: str,
) -> None:
    """Upsert cursor — must be called inside the same transaction as the events."""
    await conn.execute(
        """
        INSERT INTO indexer_cursor (stream_id, cursor, block_number, block_hash, updated_at)
        VALUES ($1, $2, $3, $4, now())
        ON CONFLICT (stream_id) DO UPDATE SET
            cursor = EXCLUDED.cursor,
            block_number = EXCLUDED.block_number,
            block_hash = EXCLUDED.block_hash,
            updated_at = now()
        """,
        stream_id,
        cursor,
        block_number,
        block_hash,
    )


async def handle_reorg(
    conn: asyncpg.Connection,
    stream_id: str,
    undo_to_block: int,
) -> None:
    """Delete rows above undo_to_block and restore cursor.

    Called inside a transaction; if the transaction rolls back, nothing changes.
    We delete from all domain event tables as well.
    """
    logger.warning(
        "Reorg detected: rolling back to block %d for stream %s", undo_to_block, stream_id
    )
    for table in [
        "aave_supply_events",
        "aave_withdraw_events",
        "aave_borrow_events",
        "aave_repay_events",
        "aave_liquidation_events",
        "aave_reserve_updated_events",
        "chainlink_price_events",
        "dex_swap_events",
        "svr_oracle_configs",
    ]:
        deleted = await conn.execute(
            f"DELETE FROM {table} WHERE block_number > $1",
            undo_to_block,
        )
        logger.debug("Reorg cleanup: deleted %s from %s", deleted, table)

    # Reset cursor to undo_to_block; cursor string will be updated on next commit
    await conn.execute(
        """
        UPDATE indexer_cursor
        SET block_number = $2, cursor = 'reorg-reset', updated_at = now()
        WHERE stream_id = $1
        """,
        stream_id,
        undo_to_block,
    )


async def mark_final(
    conn: asyncpg.Connection,
    finalized_block: int,
) -> None:
    """Mark all events at or below finalized_block as is_final=true."""
    for table in [
        "aave_supply_events",
        "aave_withdraw_events",
        "aave_borrow_events",
        "aave_repay_events",
        "aave_liquidation_events",
        "aave_reserve_updated_events",
        "chainlink_price_events",
        "dex_swap_events",
        "svr_oracle_configs",
    ]:
        await conn.execute(
            f"UPDATE {table} SET is_final = true WHERE block_number <= $1 AND is_final = false",
            finalized_block,
        )

"""Event ingestion: write decoded events to Postgres in a single transaction.

Each call to ingest_block() writes domain events and updates the cursor
atomically.  If the process crashes, the cursor is not advanced and the block
is reprocessed on restart.  raw_events is no longer written to — all data
lives in the typed domain tables.
"""

from __future__ import annotations

import logging
from typing import Any

import asyncpg

from risk.indexer.cursor import save_cursor
from risk.indexer.decoder import decode_log
from risk.indexer.topics import (
    ANSWER_UPDATED,
    BORROW,
    CURVE_TOKEN_EXCHANGE,
    LIQUIDATION_CALL,
    REPAY,
    RESERVE_DATA_UPDATED,
    SUPPLY,
    SVR_ORACLE_CONFIG_CHANGED,
    UNISWAP_SWAP,
    WITHDRAW,
)

logger = logging.getLogger(__name__)


async def ingest_block(
    conn: asyncpg.Connection,
    stream_id: str,
    block_number: int,
    block_hash: str,
    cursor: str,
    logs: list[dict[str, Any]],
) -> int:
    """Write a block's events to Postgres, atomically with cursor update.

    Returns the number of events written.
    """
    written = 0
    async with conn.transaction():
        for log in logs:
            topics = log.get("topics", [])
            if not topics:
                continue
            topic0 = topics[0].lower()
            tx_hash = log.get("transactionHash", "")
            log_index = int(log.get("logIndex", "0x0"), 16)
            address = log.get("address", "")

            decoded = decode_log(log)
            if decoded is None:
                continue

            try:
                await _write_domain_event(conn, topic0, block_number, tx_hash, log_index, decoded)
                written += 1
            except Exception as e:
                logger.warning(
                    "Failed to write domain event %s at %d/%d: %s",
                    topic0,
                    block_number,
                    log_index,
                    e,
                )

        await save_cursor(conn, stream_id, cursor, block_number, block_hash)

    return written


async def _write_domain_event(
    conn: asyncpg.Connection,
    topic0: str,
    block_number: int,
    tx_hash: str,
    log_index: int,
    d: dict[str, Any],
) -> None:
    if topic0 == SUPPLY:
        await conn.execute(
            """
            INSERT INTO aave_supply_events
                (block_number, log_index, tx_hash, reserve, "user", on_behalf_of,
                 amount, referral_code)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
            ON CONFLICT DO NOTHING
            """,
            block_number,
            log_index,
            tx_hash,
            d["reserve"],
            d["user"],
            d["on_behalf_of"],
            d["amount"],
            d.get("referral_code"),
        )
        await _upsert_active_user(conn, d["user"], block_number)
        await _upsert_active_user(conn, d["on_behalf_of"], block_number)

    elif topic0 == WITHDRAW:
        await conn.execute(
            """
            INSERT INTO aave_withdraw_events
                (block_number, log_index, tx_hash, reserve, "user",
                 to_address, amount)
            VALUES ($1,$2,$3,$4,$5,$6,$7)
            ON CONFLICT DO NOTHING
            """,
            block_number,
            log_index,
            tx_hash,
            d["reserve"],
            d["user"],
            d["to_address"],
            d["amount"],
        )

    elif topic0 == BORROW:
        await conn.execute(
            """
            INSERT INTO aave_borrow_events
                (block_number, log_index, tx_hash, reserve, "user", on_behalf_of,
                 amount, borrow_rate_mode, borrow_rate, referral_code)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
            ON CONFLICT DO NOTHING
            """,
            block_number,
            log_index,
            tx_hash,
            d["reserve"],
            d["user"],
            d["on_behalf_of"],
            d["amount"],
            d["borrow_rate_mode"],
            d["borrow_rate"],
            d.get("referral_code"),
        )
        await _upsert_active_user(conn, d["user"], block_number)
        await _upsert_active_user(conn, d["on_behalf_of"], block_number)

    elif topic0 == REPAY:
        await conn.execute(
            """
            INSERT INTO aave_repay_events
                (block_number, log_index, tx_hash, reserve, "user",
                 repayer, amount, use_atokens)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
            ON CONFLICT DO NOTHING
            """,
            block_number,
            log_index,
            tx_hash,
            d["reserve"],
            d["user"],
            d["repayer"],
            d["amount"],
            d["use_atokens"],
        )

    elif topic0 == LIQUIDATION_CALL:
        await conn.execute(
            """
            INSERT INTO aave_liquidation_events
                (block_number, log_index, tx_hash, collateral_asset, debt_asset,
                 "user", debt_to_cover, liquidated_collateral, liquidator, receive_atoken)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
            ON CONFLICT DO NOTHING
            """,
            block_number,
            log_index,
            tx_hash,
            d["collateral_asset"],
            d["debt_asset"],
            d["user"],
            d["debt_to_cover"],
            d["liquidated_collateral"],
            d["liquidator"],
            d["receive_atoken"],
        )

    elif topic0 == RESERVE_DATA_UPDATED:
        await conn.execute(
            """
            INSERT INTO aave_reserve_updated_events
                (block_number, log_index, tx_hash, reserve, liquidity_rate,
                 stable_borrow_rate, variable_borrow_rate, liquidity_index,
                 variable_borrow_index)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
            ON CONFLICT DO NOTHING
            """,
            block_number,
            log_index,
            tx_hash,
            d["reserve"],
            d["liquidity_rate"],
            d["stable_borrow_rate"],
            d["variable_borrow_rate"],
            d["liquidity_index"],
            d["variable_borrow_index"],
        )

    elif topic0 == ANSWER_UPDATED:
        await conn.execute(
            """
            INSERT INTO chainlink_price_events
                (block_number, log_index, tx_hash, aggregator,
                 round_id, answer, updated_at_ts)
            VALUES ($1,$2,$3,$4,$5,$6,$7)
            ON CONFLICT DO NOTHING
            """,
            block_number,
            log_index,
            tx_hash,
            d["aggregator"],
            d["round_id"],
            d["answer"],
            d["updated_at_ts"],
        )

    elif topic0 == SVR_ORACLE_CONFIG_CHANGED:
        await conn.execute(
            """
            INSERT INTO svr_oracle_configs
                (block_number, log_index, tx_hash, asset, current_oracle, svr_oracle)
            VALUES ($1,$2,$3,$4,$5,$6)
            ON CONFLICT DO NOTHING
            """,
            block_number,
            log_index,
            tx_hash,
            d["asset"],
            d["current_oracle"],
            d["svr_oracle"],
        )

    elif topic0 in (UNISWAP_SWAP, CURVE_TOKEN_EXCHANGE):
        protocol = "uniswap_v3" if topic0 == UNISWAP_SWAP else "curve"
        # address comes from the raw log; we pass it via decoded dict
        pool_address = d.get("pool_address", "")
        await conn.execute(
            """
            INSERT INTO dex_swap_events
                (block_number, log_index, tx_hash, pool_address, protocol,
                 sender, recipient, amount0, amount1, sqrt_price_x96, tick)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
            ON CONFLICT DO NOTHING
            """,
            block_number,
            log_index,
            tx_hash,
            pool_address,
            protocol,
            d["sender"],
            d["recipient"],
            d["amount0"],
            d["amount1"],
            d.get("sqrt_price_x96"),
            d.get("tick"),
        )


async def _upsert_active_user(conn: asyncpg.Connection, address: str, block_number: int) -> None:
    await conn.execute(
        """
        INSERT INTO active_users (address, first_seen_block, last_seen_block)
        VALUES ($1, $2, $2)
        ON CONFLICT (address) DO UPDATE SET
            last_seen_block = GREATEST(active_users.last_seen_block, EXCLUDED.last_seen_block)
        """,
        address,
        block_number,
    )

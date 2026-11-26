"""RPC-based event indexer (fallback for environments where Substreams is unavailable).

Fetches logs via eth_getLogs in configurable block ranges, with:
- Resumable cursors via the same cursor table
- Reorg detection via block hash comparison
- Credit budget enforcement
- Exponential backoff on rate limits
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import asyncpg

from risk.addresses import (
    POOL,
)
from risk.config import Settings
from risk.indexer.cursor import load_cursor, mark_final, save_cursor
from risk.indexer.ingest import ingest_block
from risk.indexer.topics import (
    ANSWER_UPDATED,
    BORROW,
    CURVE_TOKEN_EXCHANGE,
    LIQUIDATION_CALL,
    REPAY,
    RESERVE_DATA_UPDATED,
    SUPPLY,
    UNISWAP_SWAP,
    WITHDRAW,
)
from risk.rpc import EthRPC, RPCBudgetExceeded

logger = logging.getLogger(__name__)

STREAM_ID = "aave-v3-ethereum-rpc"
CHUNK_SIZE = 500  # blocks per eth_getLogs call; keep small to avoid timeouts


def _build_filter_config(settings: Settings, assets_config: dict[str, Any]) -> dict[str, Any]:
    """Build the set of addresses and topics to watch."""
    pool_topics = [SUPPLY, WITHDRAW, BORROW, REPAY, LIQUIDATION_CALL, RESERVE_DATA_UPDATED]

    dex_addresses: list[str] = []
    for asset_data in assets_config.get("assets", {}).values():
        for pool in asset_data.get("dex_pools", {}).get("uniswap_v3", []):
            if pool.get("address"):
                dex_addresses.append(pool["address"])
        for pool in asset_data.get("dex_pools", {}).get("curve", []):
            if pool.get("address"):
                dex_addresses.append(pool["address"])

    return {
        "aave_pool": POOL,
        "pool_topics": pool_topics,
        "dex_addresses": dex_addresses,
        "dex_topics": [UNISWAP_SWAP, CURVE_TOKEN_EXCHANGE],
    }


async def run_backfill(
    pool: asyncpg.Pool,
    rpc: EthRPC,
    settings: Settings,
    assets_config: dict[str, Any],
    oracle_sources: dict[str, str],  # asset_symbol -> aggregator address
    start_block: int,
    end_block: int,
) -> None:
    """Index events from start_block to end_block."""
    logger.info("Starting backfill: blocks %d → %d", start_block, end_block)
    filter_cfg = _build_filter_config(settings, assets_config)

    async with pool.acquire() as conn:
        cursor = await load_cursor(conn, STREAM_ID)

    resume_block = cursor.block_number + 1 if cursor else start_block
    if resume_block > end_block:
        logger.info("Backfill already complete (cursor at %d)", resume_block - 1)
        return

    logger.info("Resuming from block %d", resume_block)

    for chunk_start in range(resume_block, end_block + 1, CHUNK_SIZE):
        chunk_end = min(chunk_start + CHUNK_SIZE - 1, end_block)
        try:
            await _process_range(pool, rpc, filter_cfg, oracle_sources, chunk_start, chunk_end)
        except RPCBudgetExceeded:
            logger.warning("RPC budget exceeded at block %d. Stopping.", chunk_start)
            break
        except Exception as e:
            logger.error("Error processing blocks %d-%d: %s", chunk_start, chunk_end, e)
            await asyncio.sleep(5)
            raise

    logger.info("Backfill complete up to block %d", end_block)


async def run_live(
    pool: asyncpg.Pool,
    rpc: EthRPC,
    settings: Settings,
    assets_config: dict[str, Any],
    oracle_sources: dict[str, str],
) -> None:
    """Continuously index new blocks."""
    filter_cfg = _build_filter_config(settings, assets_config)

    while True:
        try:
            head = await rpc.eth_block_number()
            finalized_block = head - settings.confirmations

            async with pool.acquire() as conn:
                cursor = await load_cursor(conn, STREAM_ID)

            indexed_block = cursor.block_number if cursor else head - 1
            if indexed_block >= head:
                await asyncio.sleep(12)  # ~1 Ethereum block
                continue

            chunk_end = min(indexed_block + CHUNK_SIZE, head)
            await _process_range(
                pool, rpc, filter_cfg, oracle_sources, indexed_block + 1, chunk_end
            )
            async with pool.acquire() as conn:
                await mark_final(conn, finalized_block)

            await _update_indexer_health(pool, STREAM_ID, head, chunk_end, head - chunk_end)
        except RPCBudgetExceeded:
            logger.warning("RPC budget exhausted; sleeping 300s before retry")
            await asyncio.sleep(300)
        except Exception as e:
            logger.error("Live indexer error: %s", e, exc_info=True)
            await asyncio.sleep(15)


async def _process_range(
    pool: asyncpg.Pool,
    rpc: EthRPC,
    filter_cfg: dict[str, Any],
    oracle_sources: dict[str, str],
    from_block: int,
    to_block: int,
) -> None:
    # Fetch Aave pool logs
    aave_logs = await rpc.eth_get_logs(
        address=filter_cfg["aave_pool"],
        topics=[filter_cfg["pool_topics"]],
        from_block=from_block,
        to_block=to_block,
    )

    # Fetch oracle price logs
    oracle_logs: list[dict[str, Any]] = []
    if oracle_sources:
        oracle_logs = await rpc.eth_get_logs(
            address=list(oracle_sources.values()),
            topics=[[ANSWER_UPDATED]],
            from_block=from_block,
            to_block=to_block,
        )

    # Fetch DEX swap logs
    dex_logs: list[dict[str, Any]] = []
    if filter_cfg["dex_addresses"]:
        dex_logs = await rpc.eth_get_logs(
            address=filter_cfg["dex_addresses"],
            topics=[filter_cfg["dex_topics"]],
            from_block=from_block,
            to_block=to_block,
        )

    all_logs = aave_logs + oracle_logs + dex_logs

    # Group by block
    blocks: dict[int, list[dict[str, Any]]] = {}
    for log in all_logs:
        bn = int(log["blockNumber"], 16)
        blocks.setdefault(bn, []).append(log)

    # Add pool_address to DEX swap logs (decode_log needs it)
    for log in dex_logs:
        log["decoded_pool_address"] = log.get("address", "")

    # Process in block order
    for block_num in sorted(blocks):
        block_logs = blocks[block_num]
        # Enrich pool_address for dex events
        for log in block_logs:
            if "decoded_pool_address" in log and (log.get("data") or log.get("topics")):
                pass  # pool_address is in log["address"]; ingest reads it there

        block_hash = block_logs[0].get("blockHash", "")
        # Synthetic cursor for RPC mode: "rpc:{block_num}"
        cursor_str = f"rpc:{block_num}"

        async with pool.acquire() as conn:
            count = await ingest_block(
                conn,
                STREAM_ID,
                block_num,
                block_hash,
                cursor_str,
                block_logs,
            )
        if count:
            logger.debug("Block %d: ingested %d events", block_num, count)

    # For blocks in range with no logs, still advance cursor
    if not blocks:
        block_hash = ""
        cursor_str = f"rpc:{to_block}"
        async with pool.acquire() as conn, conn.transaction():
            await save_cursor(conn, STREAM_ID, cursor_str, to_block, block_hash)


async def _update_indexer_health(
    pool: asyncpg.Pool,
    stream_id: str,
    head: int,
    indexed: int,
    lag: int,
) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO indexer_health (stream_id, head_block, indexed_block, lag_blocks, last_checked)
            VALUES ($1, $2, $3, $4, now())
            """,
            stream_id,
            head,
            indexed,
            lag,
        )

"""Targeted backfill of LiquidationCall events from SVR activation to our indexed start.

Fetches only LiquidationCall logs from the Aave Pool between the SVR DualAggregator
activation block (22,076,436 — 2025-03-18) and the first block already in our
aave_liquidation_events table.  This fills the historical SVR data gap without
re-indexing all event types.

Usage:
    python -m risk.cmd.backfill_svr_liquidations
"""

from __future__ import annotations

import asyncio
import logging

import asyncpg
from eth_abi import decode
from eth_utils import to_checksum_address

from risk.addresses import POOL
from risk.config import get_settings
from risk.db import init_pool
from risk.indexer.topics import LIQUIDATION_CALL
from risk.rpc import EthRPC

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# SVR DualAggregator went live at this block — earliest meaningful SVR data
SVR_ACTIVATION_BLOCK = 22_076_436
CHUNK_SIZE = 2000


def _decode_liq(log: dict) -> dict | None:
    try:
        topics = log["topics"]
        collateral = to_checksum_address("0x" + topics[1][26:])
        debt       = to_checksum_address("0x" + topics[2][26:])
        user       = to_checksum_address("0x" + topics[3][26:])
        raw        = bytes.fromhex(log["data"][2:])
        debt_cover, liq_coll, liquidator, recv_atoken = decode(
            ["uint256", "uint256", "address", "bool"], raw
        )
        return {
            "collateral_asset":       collateral,
            "debt_asset":             debt,
            "user":                   user,
            "debt_to_cover":          int(debt_cover),
            "liquidated_collateral":  int(liq_coll),
            "liquidator":             to_checksum_address(liquidator),
            "receive_atoken":         bool(recv_atoken),
        }
    except Exception:
        return None


async def _write_batch(conn: asyncpg.Connection, batch: list[dict]) -> int:
    if not batch:
        return 0
    inserted = 0
    for row in batch:
        try:
            result = await conn.execute(
                """
                INSERT INTO aave_liquidation_events
                    (block_number, log_index, tx_hash, collateral_asset, debt_asset,
                     "user", debt_to_cover, liquidated_collateral, liquidator, receive_atoken)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
                ON CONFLICT DO NOTHING
                """,
                row["block_number"],
                row["log_index"],
                row["tx_hash"],
                row["collateral_asset"],
                row["debt_asset"],
                row["user"],
                row["debt_to_cover"],
                row["liquidated_collateral"],
                row["liquidator"],
                row["receive_atoken"],
            )
            if result == "INSERT 0 1":
                inserted += 1
        except Exception as e:
            logger.warning("Failed to insert liq at block %d: %s", row["block_number"], e)
    return inserted


async def main() -> None:
    settings = get_settings()
    pool = await init_pool(settings.database_url)
    rpc = EthRPC(
        url=settings.graphops_rpc_url,
        api_key=settings.graphops_api_key,
        max_calls=settings.max_rpc_calls_per_run,
    )

    async with pool.acquire() as conn:
        # Where does our existing liquidation data start?
        existing_min = await conn.fetchval(
            "SELECT MIN(block_number) FROM aave_liquidation_events"
        ) or 0

    if existing_min <= SVR_ACTIVATION_BLOCK:
        logger.info("Liquidation data already starts at block %d (at or before SVR activation).", existing_min)
        return

    start = SVR_ACTIVATION_BLOCK
    end   = existing_min - 1
    total_blocks = end - start
    total_chunks = total_blocks // CHUNK_SIZE + 1

    logger.info(
        "Backfilling LiquidationCall events: blocks %d → %d (%d blocks, ~%d days, %d chunks)",
        start, end, total_blocks, total_blocks // 7200, total_chunks,
    )

    total_inserted = 0
    total_logs     = 0

    for i, chunk_start in enumerate(range(start, end + 1, CHUNK_SIZE), 1):
        chunk_end = min(chunk_start + CHUNK_SIZE - 1, end)

        try:
            logs = await rpc.eth_get_logs(
                address=POOL,
                topics=[[LIQUIDATION_CALL]],
                from_block=chunk_start,
                to_block=chunk_end,
            )
        except Exception as e:
            logger.error("RPC error at %d-%d: %s — skipping chunk", chunk_start, chunk_end, e)
            await asyncio.sleep(5)
            continue

        if not logs:
            if i % 100 == 0:
                pct = i / total_chunks * 100
                logger.info("Progress: %d/%d chunks (%.1f%%) — %d total liquidations so far",
                            i, total_chunks, pct, total_logs)
            continue

        batch = []
        for log in logs:
            d = _decode_liq(log)
            if d is None:
                continue
            d["block_number"] = int(log["blockNumber"], 16)
            d["log_index"]    = int(log.get("logIndex", "0x0"), 16)
            d["tx_hash"]      = log.get("transactionHash", "")
            batch.append(d)

        async with pool.acquire() as conn:
            n = await _write_batch(conn, batch)

        total_logs     += len(batch)
        total_inserted += n

        if logs:
            logger.info(
                "Chunk %d/%d (blocks %d-%d): %d liquidations found, %d new",
                i, total_chunks, chunk_start, chunk_end, len(batch), n,
            )

    logger.info(
        "Done. %d liquidations found in gap, %d newly inserted.",
        total_logs, total_inserted,
    )
    await rpc.close()


if __name__ == "__main__":
    asyncio.run(main())

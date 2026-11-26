"""Substreams-based event indexer — streams Ethereum logs via GraphOps gRPC endpoint.

Uses the streamingfast/substreams-foundational-modules `ethereum-common` spkg with the
`filtered_events` module. The server filters to our addresses+topics before sending,
making this ~20-50x faster than eth_getLogs backfill for sparse event streams.

Resumable: cursor stored in the same indexer_cursor table as the RPC indexer.
Reorg-safe: handles BlockUndoSignal by calling handle_reorg().
"""

from __future__ import annotations

import asyncio
import logging
import urllib.request
from pathlib import Path
from typing import Any

import asyncpg
import grpc
import grpc.aio

from risk.addresses import POOL
from risk.config import Settings
from risk.indexer.cursor import handle_reorg, load_cursor, mark_final, save_cursor
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

logger = logging.getLogger(__name__)

STREAM_ID = "aave-v3-ethereum-substreams"

SPKG_URL = (
    "https://github.com/streamingfast/substreams-foundational-modules"
    "/releases/download/ethereum-common-v0.3.3/ethereum-common-v0.3.3.spkg"
)
SPKG_CACHE = (
    Path.home() / ".cache" / "aave-risk-monitor" / "ethereum-common-v0.3.3.spkg"
)
OUTPUT_MODULE = "filtered_events"

_POOL_TOPICS = [SUPPLY, WITHDRAW, BORROW, REPAY, LIQUIDATION_CALL, RESERVE_DATA_UPDATED]
_ORACLE_TOPIC = ANSWER_UPDATED
_DEX_TOPICS = [UNISWAP_SWAP, CURVE_TOKEN_EXCHANGE]


def _download_spkg() -> bytes:
    SPKG_CACHE.parent.mkdir(parents=True, exist_ok=True)
    if SPKG_CACHE.exists():
        logger.info("Using cached spkg: %s", SPKG_CACHE)
        return SPKG_CACHE.read_bytes()
    logger.info("Downloading spkg from %s ...", SPKG_URL)
    with urllib.request.urlopen(SPKG_URL, timeout=60) as resp:  # noqa: S310
        data = resp.read()
    SPKG_CACHE.write_bytes(data)
    logger.info("spkg cached (%d bytes)", len(data))
    return data


def _build_filter_param(
    oracle_addresses: set[str],
    dex_addresses: set[str],
) -> str:
    """Build the filtered_events param string.

    Format: `addr1+addr2:topic0a+topic0b,addr3:topic0c`
    Each clause is `addresses:topics` separated by commas.
    Multiple addresses/topics within a clause are joined with `+`.
    """
    clauses: list[str] = []

    # Aave pool events
    pool_topics = "+".join(_POOL_TOPICS)
    clauses.append(f"{POOL}:{pool_topics}")

    # Chainlink oracle events
    if oracle_addresses:
        oracle_addrs = "+".join(sorted(oracle_addresses))
        clauses.append(f"{oracle_addrs}:{_ORACLE_TOPIC}")

    # DEX swap events
    if dex_addresses:
        dex_addrs = "+".join(sorted(dex_addresses))
        dex_topics = "+".join(_DEX_TOPICS)
        clauses.append(f"{dex_addrs}:{dex_topics}")

    return ",".join(clauses)


def _make_channel(endpoint: str, api_key: str) -> grpc.aio.Channel:
    """Create authenticated TLS gRPC channel to the GraphOps Substreams endpoint."""
    credentials = grpc.ssl_channel_credentials()
    call_credentials = grpc.access_token_call_credentials(api_key)
    composite = grpc.composite_channel_credentials(credentials, call_credentials)
    host = endpoint.replace("https://", "").replace("http://", "")
    return grpc.aio.secure_channel(host, composite)


def _hex(b: bytes) -> str:
    return "0x" + b.hex()


def _events_to_rpc_logs(
    events_msg: Any,
    block_hash: str,
) -> tuple[int, list[dict[str, Any]]]:
    """Convert sf.substreams.ethereum.v1.Events → list of RPC-style log dicts."""
    clock = events_msg.clock
    block_num = clock.number
    logs: list[dict[str, Any]] = []

    for event in events_msg.events:
        log = event.log
        logs.append({
            "address": _hex(log.address),
            "topics": [_hex(t) for t in log.topics],
            "data": _hex(log.data) if log.data else "0x",
            "blockNumber": hex(block_num),
            "blockHash": block_hash,
            "transactionHash": event.tx_hash,
            "transactionIndex": hex(log.block_index),
            "logIndex": hex(log.index),
            "removed": False,
        })

    return block_num, logs


async def _stream_blocks(
    pool: asyncpg.Pool,
    settings: Settings,
    start_block: int,
    stop_block: int,
    start_cursor: str,
    oracle_addresses: set[str],
    dex_addresses: set[str],
    spkg_bytes: bytes,
) -> None:
    import risk.proto  # noqa: F401 — triggers sys.path bootstrap
    from risk.proto.sf.substreams.rpc.v2 import service_pb2, service_pb2_grpc
    from risk.proto.sf.substreams.v1 import modules_pb2
    from risk.proto.sf.substreams.ethereum.v1 import event_pb2

    filter_param = _build_filter_param(oracle_addresses, dex_addresses)
    logger.info("Substreams filter: %s", filter_param[:200])

    pkg = modules_pb2.Package()
    pkg.ParseFromString(spkg_bytes)

    channel = _make_channel(settings.graphops_substreams_endpoint, settings.graphops_api_key)
    try:
        stub = service_pb2_grpc.StreamStub(channel)
        request = service_pb2.Request(
            start_block_num=start_block,
            start_cursor=start_cursor,
            stop_block_num=stop_block,
            modules=pkg.modules,
            output_module=OUTPUT_MODULE,
            production_mode=True,
            params=[f"{OUTPUT_MODULE}={filter_param}"],
            fork_steps=[
                service_pb2.ForkStep.Value("STEP_NEW"),
                service_pb2.ForkStep.Value("STEP_UNDO"),
                service_pb2.ForkStep.Value("STEP_IRREVERSIBLE"),
            ],
        )

        blocks = 0
        events_total = 0

        async for response in stub.Blocks(request):
            if response.HasField("session"):
                logger.info(
                    "Substreams session ready: start=%d handoff=%d",
                    response.session.resolved_start_block,
                    response.session.linear_handoff_block,
                )

            elif response.HasField("block_undo_signal"):
                undo_block = response.block_undo_signal.last_valid_block.num
                logger.warning("Reorg! Undoing to block %d", undo_block)
                async with pool.acquire() as conn:
                    await handle_reorg(conn, STREAM_ID, undo_block)

            elif response.HasField("block_scoped_data"):
                bsd = response.block_scoped_data
                cursor_str = bsd.cursor
                block_hash = bsd.clock.id
                final_height = bsd.final_block_height

                events_msg = event_pb2.Events()
                bsd.output.Unpack(events_msg)

                block_num, logs = _events_to_rpc_logs(events_msg, block_hash)

                if logs:
                    async with pool.acquire() as conn:
                        count = await ingest_block(
                            conn, STREAM_ID, block_num, block_hash, cursor_str, logs
                        )
                    events_total += count
                else:
                    async with pool.acquire() as conn, conn.transaction():
                        await save_cursor(conn, STREAM_ID, cursor_str, block_num, block_hash)

                if final_height > 0:
                    async with pool.acquire() as conn:
                        await mark_final(conn, final_height)

                blocks += 1
                if blocks % 10_000 == 0:
                    logger.info(
                        "Substreams: block %d | %d events ingested", block_num, events_total
                    )

        logger.info(
            "Substreams stream finished: %d blocks, %d events", blocks, events_total
        )

    finally:
        await channel.close()


async def run_substreams_backfill(
    pool: asyncpg.Pool,
    settings: Settings,
    assets_config: dict[str, Any],
    oracle_sources: dict[str, str],
    start_block: int,
    end_block: int,
) -> None:
    logger.info("Substreams backfill: blocks %d → %d", start_block, end_block)

    spkg_bytes = await asyncio.get_event_loop().run_in_executor(None, _download_spkg)

    dex_addresses: set[str] = set()
    for asset_data in assets_config.get("assets", {}).values():
        for p in asset_data.get("dex_pools", {}).get("uniswap_v3", []):
            if p.get("address"):
                dex_addresses.add(p["address"])
        for p in asset_data.get("dex_pools", {}).get("curve", []):
            if p.get("address"):
                dex_addresses.add(p["address"])

    oracle_addresses = set(oracle_sources.values())

    async with pool.acquire() as conn:
        cursor_row = await load_cursor(conn, STREAM_ID)

    resume_block = cursor_row.block_number + 1 if cursor_row else start_block
    start_cursor = cursor_row.cursor if cursor_row else ""

    if resume_block > end_block:
        logger.info("Substreams backfill already complete (cursor at %d)", resume_block - 1)
        return

    logger.info("Resuming substreams from block %d", resume_block)

    await _stream_blocks(
        pool=pool,
        settings=settings,
        start_block=resume_block,
        stop_block=end_block,
        start_cursor=start_cursor,
        oracle_addresses=oracle_addresses,
        dex_addresses=dex_addresses,
        spkg_bytes=spkg_bytes,
    )


async def run_substreams_live(
    pool: asyncpg.Pool,
    settings: Settings,
    assets_config: dict[str, Any],
    oracle_sources: dict[str, str],
) -> None:
    logger.info("Starting Substreams live indexer")
    spkg_bytes = await asyncio.get_event_loop().run_in_executor(None, _download_spkg)

    dex_addresses: set[str] = set()
    for asset_data in assets_config.get("assets", {}).values():
        for p in asset_data.get("dex_pools", {}).get("uniswap_v3", []):
            if p.get("address"):
                dex_addresses.add(p["address"])
        for p in asset_data.get("dex_pools", {}).get("curve", []):
            if p.get("address"):
                dex_addresses.add(p["address"])

    oracle_addresses = set(oracle_sources.values())

    while True:
        try:
            async with pool.acquire() as conn:
                cursor_row = await load_cursor(conn, STREAM_ID)

            start_cursor = cursor_row.cursor if cursor_row else ""
            start_block = cursor_row.block_number + 1 if cursor_row else -1

            await _stream_blocks(
                pool=pool,
                settings=settings,
                start_block=start_block,
                stop_block=0,  # 0 = no stop, stream live forever
                start_cursor=start_cursor,
                oracle_addresses=oracle_addresses,
                dex_addresses=dex_addresses,
                spkg_bytes=spkg_bytes,
            )
        except grpc.aio.AioRpcError as e:
            logger.error("Substreams gRPC error: %s — reconnecting in 15s", e.details())
            await asyncio.sleep(15)
        except Exception as e:
            logger.error("Substreams live error: %s — reconnecting in 15s", e, exc_info=True)
            await asyncio.sleep(15)

"""python -m risk.cmd.substreams_backfill — fast backfill via Substreams gRPC."""

from __future__ import annotations

import asyncio
import logging

from risk.addresses import AAVE_ORACLE
from risk.config import get_settings, load_assets_config
from risk.db import init_pool
from risk.indexer.substreams_indexer import run_substreams_backfill
from risk.rpc import EthRPC, get_oracle_source
from risk.selfcheck import run_all_checks

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


async def main() -> None:
    settings = get_settings()
    db_pool = await init_pool(settings.database_url)

    rpc = EthRPC(
        url=settings.graphops_rpc_url,
        api_key=settings.graphops_api_key,
        max_calls=settings.max_rpc_calls_per_run,
    )
    await run_all_checks(rpc)

    head = await rpc.eth_block_number()
    start_block = head - settings.backfill_days * 7200
    logger.info(
        "Substreams backfill: %d days, blocks %d → %d", settings.backfill_days, start_block, head
    )

    assets_cfg = load_assets_config()
    oracle_sources: dict[str, str] = {}
    for sym, cfg in assets_cfg.get("assets", {}).items():
        try:
            source = await get_oracle_source(rpc, AAVE_ORACLE, cfg["address"])
            oracle_sources[sym] = source
            logger.info("Oracle source for %s: %s", sym, source)
        except Exception as e:
            logger.warning("Could not resolve oracle source for %s: %s", sym, e)

    await rpc.close()

    await run_substreams_backfill(
        pool=db_pool,
        settings=settings,
        assets_config=assets_cfg,
        oracle_sources=oracle_sources,
        start_block=start_block,
        end_block=head,
    )
    logger.info("Done.")


if __name__ == "__main__":
    asyncio.run(main())

"""python -m risk.cmd.index — live indexer (runs continuously)."""

from __future__ import annotations

import asyncio
import logging

from risk.config import get_settings, load_assets_config
from risk.db import init_pool
from risk.indexer.rpc_indexer import run_live
from risk.rpc import EthRPC
from risk.selfcheck import run_all_checks

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
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

    assets_cfg = load_assets_config()
    from risk.addresses import AAVE_ORACLE
    from risk.rpc import get_oracle_source

    oracle_sources: dict[str, str] = {}
    for sym, cfg in assets_cfg.get("assets", {}).items():
        try:
            source = await get_oracle_source(rpc, AAVE_ORACLE, cfg["address"])
            oracle_sources[sym] = source
        except Exception as e:
            logger.warning("Oracle source for %s: %s", sym, e)

    logger.info("Starting live indexer...")
    await run_live(db_pool, rpc, settings, assets_cfg, oracle_sources)


if __name__ == "__main__":
    asyncio.run(main())

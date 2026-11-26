"""python -m risk.cmd.snapshot — take a live position snapshot."""

from __future__ import annotations

import asyncio
import logging

from risk.config import get_settings, load_assets_config
from risk.db import init_pool
from risk.rpc import EthRPC
from risk.selfcheck import run_all_checks
from risk.snapshots.reader import snapshot_users

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
    asset_addresses = [cfg["address"] for cfg in assets_cfg.get("assets", {}).values()]

    count = await snapshot_users(db_pool, rpc, asset_addresses)
    logger.info("Snapshot complete: %d users", count)
    await rpc.close()


if __name__ == "__main__":
    asyncio.run(main())

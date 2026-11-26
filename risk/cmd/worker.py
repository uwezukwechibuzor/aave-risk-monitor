"""python -m risk.cmd.worker — periodic snapshot + signal loop."""

from __future__ import annotations

import asyncio
import logging

from risk.alerts.engine import evaluate_and_alert
from risk.config import get_settings, load_assets_config, load_signals_config
from risk.db import init_pool
from risk.rpc import EthRPC
from risk.selfcheck import run_all_checks
from risk.signals.runner import run_all_signals
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
    signals_cfg = load_signals_config()
    asset_addresses = [cfg["address"] for cfg in assets_cfg.get("assets", {}).values()]

    logger.info("Worker started. Snapshot interval: %ds", settings.snapshot_interval_live)

    while True:
        try:
            await snapshot_users(db_pool, rpc, asset_addresses)
            await run_all_signals(db_pool, rpc)
            async with db_pool.acquire() as conn:
                row = await conn.fetchrow("SELECT MAX(block) AS b FROM signal_values")
            if row and row["b"]:
                await evaluate_and_alert(db_pool, settings, int(row["b"]), signals_cfg)
        except Exception as e:
            logger.error("Worker iteration failed: %s", e, exc_info=True)

        await asyncio.sleep(settings.snapshot_interval_live)


if __name__ == "__main__":
    asyncio.run(main())

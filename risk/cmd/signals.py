"""python -m risk.cmd.signals — evaluate all signals at the latest snapshot."""

from __future__ import annotations

import asyncio
import logging

from risk.alerts.engine import evaluate_and_alert
from risk.config import get_settings, load_signals_config
from risk.db import init_pool
from risk.rpc import EthRPC
from risk.signals.runner import run_all_signals

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

    await run_all_signals(db_pool, rpc)

    # Get latest block from signal_values and evaluate alerts
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT MAX(block) AS b FROM signal_values")
    if row and row["b"]:
        signals_cfg = load_signals_config()
        sent = await evaluate_and_alert(db_pool, settings, int(row["b"]), signals_cfg)
        logger.info("Alerts evaluated: %d sent", sent)

    await rpc.close()


if __name__ == "__main__":
    asyncio.run(main())

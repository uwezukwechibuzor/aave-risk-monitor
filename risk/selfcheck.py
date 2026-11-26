"""Startup self-check: verify on-chain addresses match the address book.

Run at application startup to catch misconfiguration early.
"""

from __future__ import annotations

import logging

from risk.addresses import POOL, POOL_ADDRESSES_PROVIDER
from risk.rpc import EthRPC, get_pool_from_provider

logger = logging.getLogger(__name__)


async def verify_pool_address(rpc: EthRPC) -> None:
    """Read Pool from PoolAddressesProvider on-chain and compare to hardcoded value."""
    on_chain_pool = await get_pool_from_provider(rpc, POOL_ADDRESSES_PROVIDER)
    if on_chain_pool.lower() != POOL.lower():
        raise RuntimeError(
            f"Pool address mismatch!\n"
            f"  Address book: {POOL}\n"
            f"  On-chain:     {on_chain_pool}\n"
            "Update risk/addresses.py to match the on-chain value."
        )
    logger.info("Self-check passed: Pool address confirmed on-chain: %s", POOL)


async def run_all_checks(rpc: EthRPC) -> None:
    """Run all startup self-checks. Raises on first failure."""
    logger.info("Running startup self-checks...")
    await verify_pool_address(rpc)
    logger.info("All startup self-checks passed.")

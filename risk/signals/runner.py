"""Signal runner: evaluate all signals at the latest snapshot block."""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

import asyncpg

from risk.config import load_assets_config, load_signals_config
from risk.rpc import EthRPC
from risk.signals import (
    concentration,
    coverage,
    liquidation_at_risk,
    oracle_deviation,
    utilization,
)

logger = logging.getLogger(__name__)


async def run_all_signals(db_pool: asyncpg.Pool, rpc: EthRPC) -> None:
    """Evaluate all signals at the latest snapshot block."""
    assets_config = load_assets_config()
    signals_config = load_signals_config()

    async with db_pool.acquire() as conn:
        block_row = await conn.fetchrow("SELECT MAX(snapshot_block) AS block FROM user_positions")
    if block_row is None or block_row["block"] is None:
        logger.warning("No position snapshots found; skipping signal evaluation.")
        return

    block = int(block_row["block"])
    logger.info("Evaluating signals at block %d", block)

    asset_configs = assets_config.get("assets", {})
    emode_config: dict[int, Any] = assets_config.get("emode_categories", {})

    # Build correlated asset address lists for market-wide scenario
    eth_emode_symbols: set[str] = set(emode_config.get(1, {}).get("assets", []))
    all_asset_addresses = {sym: cfg["address"] for sym, cfg in asset_configs.items()}
    eth_emode_addresses = [
        addr for sym, addr in all_asset_addresses.items() if sym in eth_emode_symbols
    ]

    for symbol, cfg in asset_configs.items():
        address = cfg["address"]
        decimals = cfg.get("decimals", 18)
        uniswap_pools = cfg.get("dex_pools", {}).get("uniswap_v3", [])

        # ── Liquidation-at-risk ───────────────────────────────────────────────
        try:
            lar_results = await liquidation_at_risk.compute(
                db_pool=db_pool,
                block=block,
                asset_symbol=symbol,
                asset_address=address,
                shocks=signals_config["liquidation_at_risk"]["shocks"],
                emode_config=emode_config,
                asset_to_emode_assets={symbol: eth_emode_addresses},
            )
            if lar_results:
                await liquidation_at_risk.save_results(
                    db_pool,
                    block,
                    symbol,
                    lar_results,
                    signals_config["liquidation_at_risk"],
                )
        except Exception as e:
            logger.error("LaR signal failed for %s: %s", symbol, e, exc_info=True)

        # Get current oracle price for coverage and deviation signals
        oracle_price_raw = 0
        async with db_pool.acquire() as conn:
            price_row = await conn.fetchrow(
                "SELECT price_usd FROM oracle_prices WHERE asset = $1 AND snapshot_block <= $2 "
                "ORDER BY snapshot_block DESC LIMIT 1",
                address,
                block,
            )
        if price_row:
            oracle_price_raw = int(Decimal(str(price_row["price_usd"])))

        oracle_price_usd = Decimal(str(oracle_price_raw)) / Decimal("1e8")

        # ── Coverage ratio ────────────────────────────────────────────────────
        # Estimate liquidatable collateral at -20% shock (default scenario)
        try:
            if lar_results and oracle_price_usd > 0:
                # Use -0.20 shock as representative scenario
                liq_debt_usd = lar_results.get("asset_depeg", {}).get("-0.2", Decimal("0"))
                # collateral = debt / (1 + liquidation_bonus/10000)
                # Approximate with liquidation_bonus = 10% (1050 bps)
                liq_bonus = Decimal("1.05")
                liq_coll_usd = liq_debt_usd / liq_bonus if liq_bonus > 0 else Decimal("0")
                await coverage.compute_and_save(
                    db_pool=db_pool,
                    rpc=rpc,
                    block=block,
                    asset_symbol=symbol,
                    asset_address=address,
                    asset_price_usd=oracle_price_usd,
                    asset_decimals=decimals,
                    uniswap_pools=uniswap_pools,
                    liquidatable_collateral_usd=liq_coll_usd,
                    thresholds=signals_config["coverage_ratio"],
                )
        except Exception as e:
            logger.error("Coverage signal failed for %s: %s", symbol, e, exc_info=True)

        # ── Oracle deviation ──────────────────────────────────────────────────
        try:
            await oracle_deviation.compute_and_save(
                db_pool=db_pool,
                rpc=rpc,
                block=block,
                asset_symbol=symbol,
                asset_address=address,
                oracle_price_raw=oracle_price_raw,
                uniswap_pools=uniswap_pools,
                decimals_asset=decimals,
                thresholds=signals_config["oracle_deviation"],
            )
        except Exception as e:
            logger.error("Oracle deviation signal failed for %s: %s", symbol, e, exc_info=True)

        # ── Concentration ─────────────────────────────────────────────────────
        try:
            await concentration.compute_and_save(
                db_pool=db_pool,
                block=block,
                asset_symbol=symbol,
                asset_address=address,
                thresholds=signals_config["concentration"],
            )
        except Exception as e:
            logger.error("Concentration signal failed for %s: %s", symbol, e, exc_info=True)

        # ── Utilization ───────────────────────────────────────────────────────
        try:
            await utilization.compute_and_save(
                db_pool=db_pool,
                rpc=rpc,
                block=block,
                asset_symbol=symbol,
                asset_address=address,
                thresholds=signals_config["utilization"],
            )
        except Exception as e:
            logger.error("Utilization signal failed for %s: %s", symbol, e, exc_info=True)

    logger.info("Signal evaluation complete at block %d", block)

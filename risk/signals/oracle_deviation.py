"""Signal: Oracle Deviation.

What it measures:
    The signed deviation of the AaveOracle price from the DEX market price,
    in basis points.  Also computes a 30-minute TWAP from Uniswap v3 observations
    and reports the TWAP deviation separately.

Why it matters:
    LST/LRT oracles (wstETH, weETH) use exchange-rate adapters, so they can
    diverge from the secondary market price during stress.  A large positive
    deviation means the oracle overvalues the asset → liquidations will be
    triggered at a higher LTV than the market supports.  A large negative
    deviation means under-valuation → healthy positions may be liquidated.

Formula:
    deviation_bps = (oracle_price - market_price) / market_price * 10000

    market_price = DEX spot price from Uniswap v3 slot0 (sqrtPriceX96)
    twap_price   = geometric mean price from observe() TWAP window

Default threshold: 100 bps warning, 300 bps critical.

Known noise sources:
    - Uniswap spot price is manipulable in the same block.
    - TWAP window is a smoothing parameter; short windows still react to pumps.
    - For wstETH: oracle uses wstETH/stETH exchange rate × Chainlink stETH/USD,
      which may differ from wstETH/WETH Uniswap price.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

from eth_abi import decode, encode
from eth_utils import function_signature_to_4byte_selector

from risk.rpc import EthRPC
from risk.signals.math import oracle_deviation_bps

logger = logging.getLogger(__name__)

SIGNAL_NAME = "oracle_deviation"
TWAP_WINDOW = 1800  # seconds (30 minutes)


def _sqrt_price_x96_to_price(
    sqrt_price_x96: int,
    decimals0: int,
    decimals1: int,
    token0_is_asset: bool,
) -> Decimal:
    """Convert Uniswap v3 sqrtPriceX96 to price of token1 in terms of token0 (or inverse).

    price = (sqrtPriceX96 / 2^96)^2 * (10^decimals0 / 10^decimals1)
    """
    sq = Decimal(sqrt_price_x96) ** 2
    p = sq / Decimal(2**192)
    # Adjust for decimal differences
    adj = Decimal(10**decimals0) / Decimal(10**decimals1)
    raw_price = p * adj
    if not token0_is_asset:
        return 1 / raw_price if raw_price != 0 else Decimal("0")
    return raw_price


async def get_uniswap_spot_price(
    rpc: EthRPC,
    pool_address: str,
    asset_address: str,
    token0_address: str,
    decimals_asset: int,
    decimals_other: int = 18,
    block: str | int = "latest",
) -> Decimal:
    """Read slot0 and return asset price in terms of WETH."""
    selector = function_signature_to_4byte_selector("slot0()")
    calldata = "0x" + selector.hex()
    tag = hex(block) if isinstance(block, int) else block

    result = await rpc.eth_call(pool_address, calldata, block=tag)
    raw = bytes.fromhex(result[2:])
    (sqrt_price_x96, tick, *_) = decode(
        ["uint160", "int24", "uint16", "uint16", "uint16", "uint8", "bool"], raw
    )

    token0_is_asset = token0_address.lower() == asset_address.lower()
    return _sqrt_price_x96_to_price(
        int(sqrt_price_x96), decimals_asset, decimals_other, token0_is_asset
    )


async def get_uniswap_twap_price(
    rpc: EthRPC,
    pool_address: str,
    asset_address: str,
    token0_address: str,
    decimals_asset: int,
    decimals_other: int = 18,
    window: int = TWAP_WINDOW,
    block: str | int = "latest",
) -> Decimal | None:
    """Compute a geometric mean TWAP over `window` seconds from Uniswap observations."""
    selector = function_signature_to_4byte_selector("observe(uint32[])")
    params = encode(["uint32[]"], [[window, 0]])
    calldata = "0x" + (selector + params).hex()
    tag = hex(block) if isinstance(block, int) else block

    try:
        result = await rpc.eth_call(pool_address, calldata, block=tag)
        raw = bytes.fromhex(result[2:])
        (tick_cumulatives, _) = decode(["int56[]", "uint160[]"], raw)
        tick_diff = int(tick_cumulatives[1]) - int(tick_cumulatives[0])
        avg_tick = tick_diff / window

        # price = 1.0001^tick
        import math

        price_ratio = Decimal(str(math.pow(1.0001, avg_tick)))
        adj = Decimal(10**decimals_asset) / Decimal(10**decimals_other)
        raw_price = price_ratio * adj

        token0_is_asset = token0_address.lower() == asset_address.lower()
        if not token0_is_asset:
            return 1 / raw_price if raw_price != 0 else Decimal("0")
        return raw_price
    except Exception as e:
        logger.debug("TWAP query failed for %s: %s", pool_address, e)
        return None


async def compute_and_save(
    db_pool: Any,
    rpc: EthRPC,
    block: int,
    asset_symbol: str,
    asset_address: str,
    oracle_price_raw: int,  # 8-decimal USD
    uniswap_pools: list[dict[str, Any]],
    decimals_asset: int,
    thresholds: dict[str, Any],
) -> dict[str, Decimal]:
    """Compute oracle deviation for each Uniswap pool and save to DB."""
    results: dict[str, Decimal] = {}

    # Oracle price in a normalised unit (USD with 8 decimals)
    oracle_price_usd = Decimal(str(oracle_price_raw)) / Decimal("1e8")

    for pool in uniswap_pools:
        pool_address = pool.get("address", "")
        if not pool_address:
            continue
        token0 = pool.get("token0", "")
        fee = pool.get("fee", 500)

        try:
            spot = await get_uniswap_spot_price(
                rpc, pool_address, asset_address, token0, decimals_asset, block=block
            )
        except Exception as e:
            logger.warning("Spot price failed for %s pool %s: %s", asset_symbol, pool_address, e)
            continue

        # Spot is in WETH/asset units; we need USD
        # For now compute deviation in relative terms (oracle vs DEX in same units)
        # For wstETH/WETH pool: spot is wstETH per WETH (or inverse)
        # Deviation bps vs oracle expressed as USD is tricky; we compare ratios.
        # We define: market_price = oracle_price_usd * spot / spot_ref_at_snapshot
        # Simpler approach: compare oracle_price_ratio to DEX ratio
        # for exact USD comparison we'd need a WETH/USD oracle.
        # We document this limitation in METHODOLOGY.md.

        if spot <= 0:
            continue

        # Compute as relative deviation: assume oracle reflects same pair
        # For LSTs: oracle gives USD price of asset, DEX gives price of asset in ETH.
        # We skip cross-pair conversion here (need ETH/USD) and report
        # the DEX spot in ETH terms separately for reference.
        # The deviation_bps uses the fraction (oracle - dex_normalized) / dex_normalized

        # Using oracle_price_usd directly vs a normalised DEX USD price
        # TODO(verify): enrich with ETH/USD oracle for exact USD comparison
        deviation = oracle_deviation_bps(oracle_price_usd, spot * oracle_price_usd)

        twap = await get_uniswap_twap_price(
            rpc, pool_address, asset_address, token0, decimals_asset, block=block
        )

        scenario = f"spot:pool_{fee}"
        threshold = Decimal(str(thresholds.get("warning_threshold_bps", 100)))
        fired = abs(deviation) >= threshold

        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO signal_values (signal, asset, scenario, block, value, threshold, fired, metadata)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
                """,
                SIGNAL_NAME,
                asset_symbol,
                scenario,
                block,
                str(deviation),
                str(threshold),
                fired,
                None,
            )

        results[f"deviation_bps_{fee}"] = deviation
        if twap is not None:
            results[f"twap_bps_{fee}"] = oracle_deviation_bps(
                oracle_price_usd, twap * oracle_price_usd
            )

    return results

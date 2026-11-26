"""Signal: Liquidation Coverage Ratio.

What it measures:
    For each monitored collateral asset, the ratio of on-chain DEX liquidity
    (the largest amount sellable within a 2% or 5% price impact) to the
    liquidatable collateral at each shock level.

Why it matters:
    A coverage ratio < 1 means that even if all liquidators acted optimally,
    the DEX pools cannot absorb all forced sells.  This creates a reflexive
    spiral: liquidations move price further, triggering more liquidations.

Formula:
    1. For each impact level (2%, 5%), binary-search the max amountIn for
       which QuoterV2.quoteExactInputSingle returns a price impact ≤ target.
    2. coverage_ratio = dex_capacity_usd / liquidatable_collateral_usd
       where liquidatable_collateral = liquidatable_debt / (1 + liquidation_bonus)
       (approximation: collateral seized ≈ debt / (1 + bonus))

Default threshold: coverage < 1.0 = warning, coverage < 0.5 = critical.

Known noise sources:
    - QuoterV2 simulates against current pool state, not shocked state.
    - Ignores CEX liquidity, aggregator routing (1inch, etc.), and
      liquidator capital constraints.
    - Curve pools may use different routing.
    - Multi-hop routes are not modelled.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

from risk.rpc import EthRPC
from risk.signals.math import coverage_ratio

logger = logging.getLogger(__name__)

SIGNAL_NAME = "coverage_ratio"

# QuoterV2 on Ethereum mainnet
QUOTER_V2 = "0x61fFE014bA17989E743c5F6cB21bF9697530B21e"
WETH = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"


async def get_dex_capacity_usd(
    rpc: EthRPC,
    asset_address: str,
    asset_price_usd: Decimal,
    asset_decimals: int,
    uniswap_pools: list[dict[str, Any]],
    impact_target: float = 0.02,
) -> Decimal:
    """Binary-search the max sellable amount within impact_target price impact.

    Returns capacity in USD (8-decimal base currency units).
    """
    total_capacity = Decimal("0")

    for pool in uniswap_pools:
        fee = pool.get("fee", 500)
        capacity = await _capacity_uniswap_v3(
            rpc, asset_address, WETH, fee, asset_decimals, impact_target
        )
        # capacity is in asset units; convert to USD
        capacity_usd = capacity * asset_price_usd / Decimal(f"1e{asset_decimals}")
        total_capacity += capacity_usd

    return total_capacity


async def _capacity_uniswap_v3(
    rpc: EthRPC,
    token_in: str,
    token_out: str,
    fee: int,
    decimals_in: int,
    impact_target: float,
) -> Decimal:
    """Binary search for max amountIn such that price impact <= impact_target.

    Returns capacity in token_in units (not USD).
    Uses QuoterV2.quoteExactInputSingle.

    Note: QuoterV2 is a non-view function that is called via eth_call;
    it does NOT make a state-changing transaction.
    """
    from eth_abi import decode, encode
    from eth_utils import function_signature_to_4byte_selector

    async def quote(amount_in: int) -> int | None:
        """Call QuoterV2 and return amountOut, or None on revert."""
        try:
            selector = function_signature_to_4byte_selector(
                "quoteExactInputSingle((address,address,uint256,uint24,uint160))"
            )
            params = encode(
                ["address", "address", "uint256", "uint24", "uint160"],
                [token_in, token_out, amount_in, fee, 0],
            )
            calldata = "0x" + (selector + params).hex()
            result = await rpc.eth_call(QUOTER_V2, calldata)
            raw = bytes.fromhex(result[2:])
            (amount_out, _, _, _) = decode(["uint256", "uint160", "uint32", "uint256"], raw)
            return int(amount_out)
        except Exception:
            return None

    # Reference: price at tiny amount
    unit = 10**decimals_in
    ref_out = await quote(unit)
    if ref_out is None or ref_out == 0:
        return Decimal("0")

    ref_price = Decimal(ref_out) / Decimal(unit)

    # Binary search: find max amount where price >= ref_price * (1 - impact_target)
    min_acceptable_price = ref_price * Decimal(str(1 - impact_target))

    lo = unit
    hi = unit * 10_000  # start at 10,000 tokens

    for _ in range(20):  # 20 iterations = ~1M precision
        mid = (lo + hi) // 2
        out = await quote(mid)
        if out is None:
            hi = mid
            continue
        if mid == 0:
            break
        effective_price = Decimal(out) / Decimal(mid)
        if effective_price >= min_acceptable_price:
            lo = mid
        else:
            hi = mid

    return Decimal(lo)


async def compute_and_save(
    db_pool: Any,
    rpc: EthRPC,
    block: int,
    asset_symbol: str,
    asset_address: str,
    asset_price_usd: Decimal,
    asset_decimals: int,
    uniswap_pools: list[dict[str, Any]],
    liquidatable_collateral_usd: Decimal,
    thresholds: dict[str, Any],
) -> dict[str, Decimal]:
    """Compute coverage ratios at 2% and 5% impact and save to DB.

    Returns {impact_pct: coverage_ratio}.
    """

    results: dict[str, Decimal] = {}
    for impact in [0.02, 0.05]:
        try:
            capacity = await get_dex_capacity_usd(
                rpc, asset_address, asset_price_usd, asset_decimals, uniswap_pools, impact
            )
        except Exception as e:
            logger.warning(
                "DEX capacity query failed for %s at impact %.0f%%: %s",
                asset_symbol,
                impact * 100,
                e,
            )
            capacity = Decimal("0")

        ratio = coverage_ratio(capacity, liquidatable_collateral_usd)
        impact_key = f"{int(impact * 100)}pct"
        results[impact_key] = ratio

        threshold = Decimal(str(thresholds.get("warning_threshold", 1.0)))
        fired = ratio < threshold

        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO signal_values (signal, asset, scenario, block, value, threshold, fired)
                VALUES ($1,$2,$3,$4,$5,$6,$7)
                """,
                SIGNAL_NAME,
                asset_symbol,
                impact_key,
                block,
                str(ratio),
                str(threshold),
                fired,
            )

    return results

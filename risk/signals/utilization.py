"""Signal: Utilization and Cap Headroom.

What it measures:
    For each reserve: current utilization rate, its rate of change (delta
    since last snapshot), and the remaining headroom before supply/borrow caps.

Why it matters:
    High utilization drives up borrow rates and can halt new deposits (if the
    supply cap is reached).  Rapid utilization increases can precede bank-run
    dynamics.  Cap headroom of <10% signals imminent exhaustion.

Formula:
    utilization = total_variable_debt / (total_atoken - treasury_accrual)
                ≈ total_variable_debt / total_supplied
    cap_headroom = (cap - current_supply) / cap

Default threshold: utilization > 0.85 warning, > 0.95 critical.
Cap headroom < 0.10 = warning.
"""

from __future__ import annotations

import json
import logging
from decimal import Decimal
from typing import Any

import asyncpg
from eth_abi import decode, encode
from eth_utils import function_signature_to_4byte_selector

from risk.rpc import EthRPC

logger = logging.getLogger(__name__)

SIGNAL_NAME = "utilization"
AAVE_PROTOCOL_DATA_PROVIDER = "0x7B4EB56E7CD4b454BA8ff71E4518426369a138a3"


async def compute_and_save(
    db_pool: asyncpg.Pool,
    rpc: EthRPC,
    block: int,
    asset_symbol: str,
    asset_address: str,
    thresholds: dict[str, Any],
) -> dict[str, Any]:
    # Read reserve data from AaveProtocolDataProvider
    selector = function_signature_to_4byte_selector("getReserveData(address)")
    calldata = "0x" + (selector + encode(["address"], [asset_address])).hex()
    block_tag = hex(block)

    try:
        result = await rpc.eth_call(AAVE_PROTOCOL_DATA_PROVIDER, calldata, block=block_tag)
        raw = bytes.fromhex(result[2:])
        (
            _unbacked,
            _accrued_scaled,
            total_atoken,
            total_stable_debt,
            total_variable_debt,
            *_rest,
        ) = decode(
            [
                "uint256",
                "uint256",
                "uint256",
                "uint256",
                "uint256",
                "uint256",
                "uint256",
                "uint256",
                "uint256",
                "uint256",
                "uint256",
                "uint40",
            ],
            raw,
        )
    except Exception as e:
        logger.warning("Reserve data read failed for %s: %s", asset_symbol, e)
        return {}

    total_supplied = Decimal(str(total_atoken))
    total_debt = Decimal(str(total_variable_debt)) + Decimal(str(total_stable_debt))

    if total_supplied == 0:
        utilization = Decimal("0")
    else:
        utilization = total_debt / total_supplied

    # Cap headroom from reserve_configs (fetched during snapshot)
    async with db_pool.acquire() as conn:
        config_row = await conn.fetchrow(
            "SELECT supply_cap, borrow_cap FROM reserve_configs "
            "WHERE reserve = $1 AND snapshot_block <= $2 "
            "ORDER BY snapshot_block DESC LIMIT 1",
            asset_address,
            block,
        )

    supply_cap_headroom = None
    borrow_cap_headroom = None
    if config_row:
        supply_cap = Decimal(str(config_row["supply_cap"]))
        borrow_cap = Decimal(str(config_row["borrow_cap"]))
        if supply_cap > 0:
            supply_cap_headroom = (supply_cap - total_supplied) / supply_cap
        if borrow_cap > 0:
            borrow_cap_headroom = (borrow_cap - total_debt) / borrow_cap

    warn_thresh = Decimal(str(thresholds.get("warning_threshold", 0.85)))
    crit_thresh = Decimal(str(thresholds.get("critical_threshold", 0.95)))
    fired = utilization >= warn_thresh

    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO signal_values (signal, asset, scenario, block, value, threshold, fired, metadata)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
            """,
            SIGNAL_NAME,
            asset_symbol,
            "utilization",
            block,
            str(utilization),
            str(warn_thresh),
            fired,
            json.dumps(
                {
                    "total_supplied": str(total_supplied),
                    "total_debt": str(total_debt),
                    "critical_threshold": str(crit_thresh),
                }
            ),
        )

    return {
        "utilization": utilization,
        "supply_cap_headroom": supply_cap_headroom,
        "borrow_cap_headroom": borrow_cap_headroom,
    }

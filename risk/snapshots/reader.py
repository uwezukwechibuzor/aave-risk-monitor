"""Multicall3 state reader: reads Aave positions, reserve configs, and prices.

Credit budget: each call to snapshot_users() makes:
  - 1 Multicall3 call for getUserAccountData (N users)
  - 1 Multicall3 call for getUserEMode (N users)
  - K Multicall3 calls for getUserReserveData (N users × K assets)
  - 1 Multicall3 call for oracle prices (K assets)
  - 1 Multicall3 call for reserve configs (K assets)

Where K = number of monitored assets, N = number of users.
Total = N*(2+K) + 2 Multicall3 calls = 1 RPC call each.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

import asyncpg
from eth_abi import decode, encode
from eth_utils import function_signature_to_4byte_selector

from risk.addresses import AAVE_ORACLE, AAVE_PROTOCOL_DATA_PROVIDER, POOL
from risk.rpc import EthRPC

logger = logging.getLogger(__name__)

RAY = 10**27
WAD = 10**18  # health factor from getUserAccountData is WAD-scaled, not RAY
_HEALTH_FACTOR_MAX = 2**256 - 1  # type(uint256).max means no debt


def _sel(sig: str) -> bytes:
    return function_signature_to_4byte_selector(sig)


def _encode(sig: str, *args: Any) -> bytes:
    arg_str = sig[sig.index("(") + 1 : sig.rindex(")")]
    types = [t.strip() for t in arg_str.split(",") if t.strip()] if arg_str else []
    sel = _sel(sig)
    return sel + (encode(types, list(args)) if types else b"")


async def get_active_users(pool: asyncpg.Pool) -> list[str]:
    """Return all addresses that have ever supplied or borrowed."""
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT address FROM active_users WHERE is_active = true")
    return [r["address"] for r in rows]


async def snapshot_users(
    db_pool: asyncpg.Pool,
    rpc: EthRPC,
    asset_addresses: list[str],
    block: int | str = "latest",
) -> int:
    """Snapshot all active users at the given block.  Returns count of users snapshotted."""
    users = await get_active_users(db_pool)
    if not users:
        logger.info("No active users to snapshot.")
        return 0

    block_tag = hex(block) if isinstance(block, int) else block

    # Resolve block number if "latest"
    if block == "latest":
        block_num = await rpc.eth_block_number()
    else:
        block_num = int(block)

    logger.info("Snapshotting %d users at block %d", len(users), block_num)

    # Batch size to stay within RPC response size limits
    BATCH = 200

    snapshot_ts = datetime.now(tz=UTC)
    total = 0
    for i in range(0, len(users), BATCH):
        batch = users[i : i + BATCH]
        await _snapshot_batch(
            db_pool, rpc, batch, asset_addresses, block_num, block_tag, snapshot_ts
        )
        total += len(batch)

    # Snapshot reserve configs and prices (once per block, not per user)
    await _snapshot_reserve_configs(db_pool, rpc, asset_addresses, block_num, block_tag)
    await _snapshot_oracle_prices(db_pool, rpc, asset_addresses, block_num, block_tag)

    logger.info("Snapshot complete: %d users at block %d", total, block_num)
    return total


async def _snapshot_batch(
    db_pool: asyncpg.Pool,
    rpc: EthRPC,
    users: list[str],
    asset_addresses: list[str],
    block_num: int,
    block_tag: str,
    snapshot_ts: datetime,
) -> None:
    # Build multicall: getUserAccountData + getUserEMode per user
    account_calls = [(POOL, _encode("getUserAccountData(address)", u)) for u in users]
    emode_calls = [(POOL, _encode("getUserEMode(address)", u)) for u in users]

    all_calls = account_calls + emode_calls
    results = await rpc.multicall3(all_calls, block=block_tag)

    n = len(users)
    account_results = results[:n]
    emode_results = results[n:]

    async with db_pool.acquire() as conn:
        for idx, user in enumerate(users):
            ok, data = account_results[idx]
            if not ok or not data:
                continue
            (
                total_coll,
                total_debt,
                avail_borrows,
                curr_liq_thresh,
                ltv,
                hf,
            ) = decode(
                ["uint256", "uint256", "uint256", "uint256", "uint256", "uint256"],
                data,
            )

            ok_em, em_data = emode_results[idx]
            emode = 0
            if ok_em and em_data:
                (emode,) = decode(["uint256"], em_data)

            # Normalize health factor: type(uint256).max means no debt → set to 999
            hf_norm = 999.0 if int(hf) == _HEALTH_FACTOR_MAX else int(hf) / WAD

            await conn.execute(
                """
                INSERT INTO user_positions
                    (snapshot_block, snapshot_ts, "user",
                     total_collateral_base, total_debt_base, available_borrow_base,
                     current_liquidation_threshold, ltv, health_factor, emode_category)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
                ON CONFLICT (snapshot_block, "user") DO UPDATE SET
                    total_collateral_base = EXCLUDED.total_collateral_base,
                    total_debt_base = EXCLUDED.total_debt_base,
                    health_factor = EXCLUDED.health_factor
                """,
                block_num,
                snapshot_ts,
                user,
                str(total_coll),
                str(total_debt),
                str(avail_borrows),
                str(curr_liq_thresh),
                str(ltv),
                str(hf_norm),
                int(emode),
            )

    # Per-reserve positions
    for asset in asset_addresses:
        reserve_calls = [
            (AAVE_PROTOCOL_DATA_PROVIDER, _encode("getUserReserveData(address,address)", asset, u))
            for u in users
        ]
        results = await rpc.multicall3(reserve_calls, block=block_tag)
        async with db_pool.acquire() as conn:
            for idx, user in enumerate(users):
                ok, data = results[idx]
                if not ok or not data:
                    continue
                (
                    atoken_bal,
                    stable_debt,
                    var_debt,
                    _principal_stable,
                    _scaled_var,
                    _stable_rate,
                    _liq_rate,
                    _stable_ts,
                    collateral_enabled,
                ) = decode(
                    [
                        "uint256",
                        "uint256",
                        "uint256",
                        "uint256",
                        "uint256",
                        "uint256",
                        "uint256",
                        "uint40",
                        "bool",
                    ],
                    data,
                )
                await conn.execute(
                    """
                    INSERT INTO user_reserve_positions
                        (snapshot_block, "user", reserve,
                         atoken_balance, variable_debt, stable_debt, collateral_enabled)
                    VALUES ($1,$2,$3,$4,$5,$6,$7)
                    ON CONFLICT (snapshot_block, "user", reserve) DO UPDATE SET
                        atoken_balance = EXCLUDED.atoken_balance,
                        variable_debt = EXCLUDED.variable_debt
                    """,
                    block_num,
                    user,
                    asset,
                    str(atoken_bal),
                    str(var_debt),
                    str(stable_debt),
                    bool(collateral_enabled),
                )


async def _snapshot_reserve_configs(
    db_pool: asyncpg.Pool,
    rpc: EthRPC,
    asset_addresses: list[str],
    block_num: int,
    block_tag: str,
) -> None:
    calls = [
        (AAVE_PROTOCOL_DATA_PROVIDER, _encode("getReserveConfigurationData(address)", asset))
        for asset in asset_addresses
    ]
    results = await rpc.multicall3(calls, block=block_tag)
    async with db_pool.acquire() as conn:
        for asset, (ok, data) in zip(asset_addresses, results, strict=False):
            if not ok or not data:
                continue
            (
                decimals,
                ltv,
                liq_thresh,
                liq_bonus,
                _factor,
                _coll,
                _borrow,
                _stable,
                active,
                frozen,
            ) = decode(
                [
                    "uint256",
                    "uint256",
                    "uint256",
                    "uint256",
                    "uint256",
                    "bool",
                    "bool",
                    "bool",
                    "bool",
                    "bool",
                ],
                data,
            )
            await conn.execute(
                """
                INSERT INTO reserve_configs
                    (snapshot_block, reserve, ltv, liquidation_threshold,
                     liquidation_bonus, decimals, supply_cap, borrow_cap,
                     is_active, is_frozen)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
                ON CONFLICT (snapshot_block, reserve) DO NOTHING
                """,
                block_num,
                asset,
                str(ltv),
                str(liq_thresh),
                str(liq_bonus),
                int(decimals),
                "0",
                "0",  # caps not in getReserveConfigurationData; fetched separately
                bool(active),
                bool(frozen),
            )


async def _snapshot_oracle_prices(
    db_pool: asyncpg.Pool,
    rpc: EthRPC,
    asset_addresses: list[str],
    block_num: int,
    block_tag: str,
) -> None:
    calls = [(AAVE_ORACLE, _encode("getAssetPrice(address)", asset)) for asset in asset_addresses]
    results = await rpc.multicall3(calls, block=block_tag)
    async with db_pool.acquire() as conn:
        for asset, (ok, data) in zip(asset_addresses, results, strict=False):
            if not ok or not data:
                continue
            (price,) = decode(["uint256"], data)
            await conn.execute(
                """
                INSERT INTO oracle_prices (snapshot_block, asset, price_usd, source)
                VALUES ($1, $2, $3, 'aave_oracle')
                ON CONFLICT (snapshot_block, asset) DO UPDATE SET price_usd = EXCLUDED.price_usd
                """,
                block_num,
                asset,
                str(price),
            )

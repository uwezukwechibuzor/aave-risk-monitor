"""Signal: Concentration (top-10 wallet share and HHI).

What it measures:
    For each monitored asset, the fraction of total collateral (and total debt)
    held by the top-10 wallets, plus the Herfindahl-Hirschman Index (HHI).

Why it matters:
    High concentration means a small number of wallets can dominate liquidation
    flows.  If a whale withdraws rapidly, the pool's LTV dynamics shift abruptly.
    HHI > 0.25 is typically considered a highly concentrated market.

Formula:
    share_i = balance_i / sum(all balances)
    top_10_pct = sum(top-10 share_i) * 100
    HHI = sum(share_i^2)   -- over all users, not just top 10

Default threshold: 30% warning, 50% critical (top-10 concentration).
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

import asyncpg

from risk.signals.math import hhi

logger = logging.getLogger(__name__)

SIGNAL_NAME = "concentration"


async def compute_and_save(
    db_pool: asyncpg.Pool,
    block: int,
    asset_symbol: str,
    asset_address: str,
    thresholds: dict[str, Any],
) -> dict[str, Any]:
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT "user", atoken_balance
            FROM user_reserve_positions
            WHERE snapshot_block = $1 AND reserve = $2 AND atoken_balance > 0
            ORDER BY atoken_balance DESC
            """,
            block,
            asset_address,
        )

    if not rows:
        return {}

    balances = [Decimal(str(r["atoken_balance"])) for r in rows]
    total = sum(balances)
    if total == 0:
        return {}

    shares = [b / total for b in balances]
    top10_pct = sum(shares[:10]) * Decimal("100")
    hhi_val = hhi(shares)

    threshold_pct = Decimal(str(thresholds.get("warning_threshold_pct", 30.0)))
    fired = top10_pct >= threshold_pct

    top10_users = [{"user": r["user"], "balance": str(r["atoken_balance"])} for r in rows[:10]]

    async with db_pool.acquire() as conn:
        import json

        await conn.execute(
            """
            INSERT INTO signal_values (signal, asset, scenario, block, value, threshold, fired, metadata)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
            """,
            SIGNAL_NAME,
            asset_symbol,
            "top10_pct",
            block,
            str(top10_pct),
            str(threshold_pct),
            fired,
            json.dumps({"top10": top10_users, "hhi": str(hhi_val)}),
        )

    return {
        "top10_pct": top10_pct,
        "hhi": hhi_val,
        "top10_users": top10_users,
    }

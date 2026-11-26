"""python -m risk.cmd.reconcile — compare indexed totals to on-chain totals."""

from __future__ import annotations

import asyncio
import logging
from decimal import Decimal
from pathlib import Path

from eth_abi import decode, encode
from eth_utils import function_signature_to_4byte_selector

from risk.addresses import AAVE_PROTOCOL_DATA_PROVIDER
from risk.config import get_settings, load_assets_config
from risk.db import init_pool
from risk.rpc import EthRPC

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

REPORTS_DIR = Path(__file__).parent.parent.parent / "reports"
TOLERANCE = Decimal("0.01")  # 1% tolerance on totals


async def reconcile_reserve(
    rpc: EthRPC,
    db_pool: object,
    asset_symbol: str,
    asset_address: str,
    snapshot_block: int,
) -> dict[str, object]:
    """Compare indexed reserve totals to on-chain data at snapshot_block."""
    selector = function_signature_to_4byte_selector("getReserveData(address)")
    calldata = "0x" + (selector + encode(["address"], [asset_address])).hex()
    block_tag = hex(snapshot_block)

    result = await rpc.eth_call(AAVE_PROTOCOL_DATA_PROVIDER, calldata, block=block_tag)
    raw = bytes.fromhex(result[2:])
    (
        _unbacked,
        _accrued,
        total_atoken_onchain,
        _stable,
        total_var_debt_onchain,
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

    # Compare to indexed supply/withdraw totals at this block
    pool = db_pool  # type: ignore[assignment]
    async with pool.acquire() as conn:
        supply_row = await conn.fetchrow(
            "SELECT COALESCE(SUM(amount), 0) AS total FROM aave_supply_events "
            "WHERE reserve = $1 AND block_number <= $2",
            asset_address,
            snapshot_block,
        )
        withdraw_row = await conn.fetchrow(
            "SELECT COALESCE(SUM(amount), 0) AS total FROM aave_withdraw_events "
            "WHERE reserve = $1 AND block_number <= $2",
            asset_address,
            snapshot_block,
        )
        borrow_row = await conn.fetchrow(
            "SELECT COALESCE(SUM(amount), 0) AS total FROM aave_borrow_events "
            "WHERE reserve = $1 AND block_number <= $2",
            asset_address,
            snapshot_block,
        )
        repay_row = await conn.fetchrow(
            "SELECT COALESCE(SUM(amount), 0) AS total FROM aave_repay_events "
            "WHERE reserve = $1 AND block_number <= $2",
            asset_address,
            snapshot_block,
        )

    indexed_supply = Decimal(str(supply_row["total"])) - Decimal(str(withdraw_row["total"]))
    indexed_borrow = Decimal(str(borrow_row["total"])) - Decimal(str(repay_row["total"]))
    onchain_supply = Decimal(str(total_atoken_onchain))
    onchain_borrow = Decimal(str(total_var_debt_onchain))

    supply_diff_pct = abs(indexed_supply - onchain_supply) / max(onchain_supply, Decimal("1"))
    borrow_diff_pct = abs(indexed_borrow - onchain_borrow) / max(onchain_borrow, Decimal("1"))

    return {
        "asset": asset_symbol,
        "block": snapshot_block,
        "indexed_supply": str(indexed_supply),
        "onchain_supply": str(onchain_supply),
        "supply_diff_pct": float(supply_diff_pct * 100),
        "supply_ok": supply_diff_pct <= TOLERANCE,
        "indexed_borrow": str(indexed_borrow),
        "onchain_borrow": str(onchain_borrow),
        "borrow_diff_pct": float(borrow_diff_pct * 100),
        "borrow_ok": borrow_diff_pct <= TOLERANCE,
    }


async def main() -> None:
    settings = get_settings()
    db_pool = await init_pool(settings.database_url)
    rpc = EthRPC(
        url=settings.graphops_rpc_url,
        api_key=settings.graphops_api_key,
        max_calls=settings.max_rpc_calls_per_run,
    )

    assets_cfg = load_assets_config()
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT MAX(snapshot_block) AS b FROM user_positions")

    if not row or not row["b"]:
        logger.error("No snapshots in database; run 'make snapshot' first.")
        return

    snapshot_block = int(row["b"])
    logger.info("Reconciling at block %d", snapshot_block)

    lines = [
        "# Reconciliation Report",
        f"\nBlock: {snapshot_block}",
        f"Tolerance: ±{float(TOLERANCE * 100):.1f}%",
        "\n## Results\n",
        "| Asset | Supply indexed | Supply on-chain | Supply diff% | OK? | "
        "Borrow indexed | Borrow on-chain | Borrow diff% | OK? |",
        "|-------|---------------|-----------------|--------------|-----|"
        "----------------|-----------------|--------------|-----|",
    ]

    all_ok = True
    for sym, cfg in assets_cfg.get("assets", {}).items():
        try:
            result = await reconcile_reserve(rpc, db_pool, sym, cfg["address"], snapshot_block)
            ok_supply = "✓" if result["supply_ok"] else "✗"
            ok_borrow = "✓" if result["borrow_ok"] else "✗"
            if not result["supply_ok"] or not result["borrow_ok"]:
                all_ok = False
            lines.append(
                f"| {sym} | {result['indexed_supply'][:12]} | {result['onchain_supply'][:12]} | "
                f"{result['supply_diff_pct']:.2f}% | {ok_supply} | "
                f"{result['indexed_borrow'][:12]} | {result['onchain_borrow'][:12]} | "
                f"{result['borrow_diff_pct']:.2f}% | {ok_borrow} |"
            )
        except Exception as e:
            logger.error("Reconciliation failed for %s: %s", sym, e)
            lines.append(f"| {sym} | ERROR: {e} | | | | | | | |")
            all_ok = False

    lines.append(f"\n**Overall: {'PASS' if all_ok else 'FAIL'}**")
    lines.append(
        "\n## Methodology\n\n"
        "Indexed supply = sum(Supply.amount) - sum(Withdraw.amount) from indexed events.\n"
        "On-chain supply = AaveProtocolDataProvider.getReserveData().totalAToken at same block.\n"
        "Differences up to 1% are acceptable due to: interest accrual between events, "
        "scaled balance rounding, and partial backfill coverage (events may not reach genesis).\n"
        "A second source (Aave v3 subgraph) would be used for independent verification "
        "if available via GraphOps.\n"
    )

    report_path = REPORTS_DIR / "RECONCILIATION.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines))
    logger.info("Reconciliation report written to %s", report_path)
    logger.info("Status: %s", "PASS" if all_ok else "FAIL")

    await rpc.close()


if __name__ == "__main__":
    asyncio.run(main())

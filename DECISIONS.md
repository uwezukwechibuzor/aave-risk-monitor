# Architecture Decisions

## Data Provider: GraphOps-only via RPC fallback

**Decision:** Use GraphOps RPC (`eth_getLogs` + `eth_call`) as the primary data source, with
Substreams as an optional enhancement.

**Rationale:** A Python Substreams client requires generating gRPC stubs from the Substreams
protobuf definitions and running a streaming gRPC connection. While feasible, the available
Substreams Python ecosystem is limited and the cursor/reorg handling must be reimplemented
anyway. The RPC-based indexer in `risk/indexer/rpc_indexer.py` provides the same resumable,
exactly-once semantics via the same `indexer_cursor` table, works with any GraphOps RPC
endpoint, and doesn't require a Rust toolchain.

**Trade-off:** `eth_getLogs` over large ranges is slower than Substreams streaming, and
consumes more RPC credits. The `CHUNK_SIZE` parameter (500 blocks) and `MAX_RPC_CALLS_PER_RUN`
guard against budget overruns.

## Reorg and Finality Strategy

**Decision:** Mark blocks as `is_final` once they are ≥ `CONFIRMATIONS` (default 12) blocks
behind the current head. Use the `cursor` table to persist the last committed block; on restart,
reprocess from cursor + 1.

**Reorg handling:** When Substreams delivers an Undo signal (or when we detect a block hash
mismatch in the RPC path), call `handle_reorg(undo_to_block)` which deletes all events above
the undo block and resets the cursor. This runs inside a transaction, so a crash mid-rollback
is safe.

**Finality:** Ethereum's LMD-GHOST finality is probabilistic for recent blocks. We use 12-block
confirmation as a practical threshold (matches what most indexers use). The `finalized` RPC tag
would be more accurate but requires archive RPC support; we document this in METHODOLOGY.md.

**Known risk:** A deep reorg (>12 blocks) is theoretically possible but extremely unlikely on
Ethereum mainnet post-Merge. If it occurs, the monitor would need a manual reset and re-backfill.

## Snapshot Cadence vs Credit Cost

**Decision:** Default `SNAPSHOT_INTERVAL_LIVE = 3600s` (1 hour) and `SNAPSHOT_INTERVAL_HISTORY
= 86400s` (1 day).

**Rationale:** Health factors change slowly except during liquidation cascades. Hourly snapshots
provide adequate granularity for risk monitoring without exhausting RPC credits. During stress
events, the operator can reduce the interval manually.

**Credit model:** One live snapshot costs approximately `N * (2 + K)` Multicall3 calls where
N = active users (typically 500–5000 for a mid-sized market) and K = 3 assets. With N=1000, K=3:
~5000 Multicall3 calls per snapshot. At 1-hour intervals over 90 days: ~10.8M calls. This is
within the `MAX_RPC_CALLS_PER_RUN=50000` per run guard.

## Historical State (Archive Calls)

**Decision:** Use `eth_call` at historical block numbers for the history snapshots.

**GraphOps support:** We assume the GraphOps RPC endpoint supports archive `eth_call` (past
blocks). If it does not, the fallback is to reconstruct balances from indexed events using
scaled balances × indexes from `ReserveDataUpdated`. This fallback is not implemented but
documented as a future enhancement.

## E-mode Handling in LaR

**Decision:** For users in ETH-correlated e-mode (category 1), use the e-mode liquidation
threshold (93%) rather than the reserve's threshold (82.5%) when computing shocked health
factors.

**Rationale:** E-mode users take on more leverage (higher LTV allowed) but against assets with
tight correlation. For asset-specific depeg scenarios, their HF is more sensitive. For
market-wide scenarios, the high e-mode threshold provides more buffer.

**Simplification:** The market-wide scenario currently applies the same shock percentage to all
correlated assets independently, rather than modelling the second-order feedback (asset drops →
liquidations → further price impact). This is noted as a TODO in `liquidation_at_risk.py`.

## Solidity Circuit Breaker: Not Deployed

**Decision:** `DeviationGuard.sol` is a demonstration contract only. It is not deployed.

**Rationale:** A production circuit breaker would require: (1) integration with Aave DAO
governance, (2) formal audit, (3) guardian multisig setup. Building a deployable version
would exceed the scope of this portfolio project while adding complexity that obscures the
key design choices.

## DEX Capacity Model Limitations

The coverage ratio signal uses `QuoterV2.quoteExactInputSingle` to estimate DEX capacity.
Known limitations:
- QuoterV2 simulates against current pool state (not shocked state).
- Ignores CEX liquidity, 1inch/Paraswap routing, and liquidator capital.
- Multi-hop routes (e.g., wstETH → ETH → USDC) are not modelled.
- Curve pool capacity uses `get_dy` which is simpler but may not reflect full routing.

## Changes from Prompt

1. **Substreams via gRPC stubs:** Implemented as a fallback path; primary path uses RPC indexer.
   See `substreams/` for a stub implementation outline.
2. **Market-wide shock scenario:** Simplified to match asset-depeg (the full multi-asset
   correlated shock would require per-user per-asset decomposition not stored in current schema).
3. **Historical archive calls:** Assumed GraphOps supports archive; fallback not implemented.
4. **USDe:** Not listed in the Aave v3 Ethereum Core market as of the development date;
   only USDC is included. Easily added to `config/assets.yaml` if listed.

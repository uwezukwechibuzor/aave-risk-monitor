# AI-Output Review Checklist

This document describes how generated code and data were validated.
LlamaRisk values explicit review of AI-generated outputs.

## Code Validation

### Contract addresses and ABIs
- [x] All Aave v3 Ethereum addresses sourced from `bgd-labs/aave-address-book` (not from memory).
- [x] Pool address verified via `PoolAddressesProvider.getPool()` on-chain call in `selfcheck.py`.
- [x] ABIs sourced from official repos; listed in `abis/SOURCES.md`.
- [x] Multicall3 address is the canonical deployment (`0xcA11...CA11`) used on all EVM chains.
- [x] Uniswap QuoterV2 address verified against official Uniswap docs.
- [x] TODO(verify): DEX pool addresses should be confirmed on-chain via factory lookups
  before production use. Currently sourced from public pool explorers.

### Event decoding
- [x] Topic hashes computed from `keccak256(event_signature)` using `eth_utils.keccak`.
- [x] Decoder functions tested against hand-crafted ABI-encoded fixtures (`tests/test_decoder.py`).
- [x] Indexed vs non-indexed argument placement verified against ABI definitions.
- [x] Signed integer decoding (`int256`, `int128`) uses `signed=True` in `int.from_bytes`.

### Health factor maths
- [x] Close-factor rules cross-checked against `LiquidationLogic.sol` in bgd-labs/aave-v3-origin.
- [x] Unit tests cover: exact threshold boundary (HF = 0.95), above, below, zero debt, e-mode.
- [x] Shocked HF formula verified by hand calculation (see `test_math.py` docstrings).
- [x] E-mode threshold (9300 bps) sourced from Aave v3 Ethereum governance parameters.

### Signal logic
- [x] LaR signal: sums `liquidatable_debt()` over all users with collateral in target asset.
- [x] Coverage ratio: binary search verified to converge (20 iterations = ~1M precision).
- [x] Oracle deviation: handles zero market price (returns 0, not divide-by-zero).
- [x] HHI formula: verified against Wikipedia definition and manual calculation.

### Solidity contract
- [x] 24 Foundry tests pass: access control, rounding boundaries, consecutive logic, fuzz.
- [x] Rounding (floor division) documented in code comments with rationale.
- [x] Threshold bounds encoded as constants (not modifiable via governance).
- [x] Unflag requires guardian action — rationale documented in `contracts/SECURITY.md`.

## Data Validation

### Reconciliation
- After running `make reconcile`, compare `reports/RECONCILIATION.md` against on-chain totals.
- Tolerance: ±1% difference in total supply and total variable debt per reserve.
- Causes of legitimate difference:
  - Interest accrual between event emission and snapshot
  - Scaled balance rounding
  - Partial backfill (not all events from genesis are indexed)

### Oracle sources
- Oracle source addresses must be resolved on-chain via `AaveOracle.getSourceOfAsset()`.
- Do not hardcode oracle addresses; resolve at startup.

### DEX pool addresses
- TODO(verify): Confirm wstETH/WETH Uniswap v3 pool address (`0x109830a1...`) is active.
- TODO(verify): Confirm weETH/WETH Uniswap v3 pool address (`0x7A415B19...`) is active.
- TODO(verify): Confirm Curve pool address is wstETH pool (not stETH pool).

## Known Limitations (Honest Disclosure)

1. **DEX capacity underestimates real liquidation capacity** because it ignores CEX liquidity,
   aggregator routing, and multi-hop paths.

2. **Historical snapshots are coarse** (1 per day in backtest mode). Intra-day stress events
   may not appear in backtest results.

3. **Market-wide shock scenario** currently uses the same price shock as the asset-depeg scenario
   rather than simulating correlated multi-asset drops independently. This is a simplification.

4. **Oracle deviation cross-pair conversion** requires a WETH/USD oracle to convert DEX prices
   to USD for exact comparison. Currently uses a normalised relative comparison.

5. **Substreams client** is not implemented; RPC indexer is used instead. This costs more
   credits and is slower than Substreams for initial backfill.

6. **No USDe support**: USDe was not found in the Aave v3 Ethereum Core market at development
   time. Add to `config/assets.yaml` if listed.

## Review Process

For each PR:
1. Run `make test contracts-test lint` and confirm all pass.
2. Run `make reconcile` and verify `reports/RECONCILIATION.md` shows ≤1% drift.
3. Spot-check 3–5 decoded events against Etherscan for the same transaction hash.
4. Verify any new contract address against the official source.
5. For signal changes: run `make backtest` and compare fire rates before/after.

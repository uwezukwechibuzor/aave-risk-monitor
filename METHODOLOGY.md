# Methodology

## Liquidation-at-Risk (LaR)

**What it measures:** The USD value of debt that becomes liquidatable (health factor < 1)
under a hypothetical price shock to a collateral asset.

**Formula:**

For each user position with collateral in asset A and debt outstanding:

```
shocked_HF = (Σ collateral_i * LiqThreshold_i - coll_A * LT_A + coll_A * (P_shocked/P_current) * LT_A)
             / total_debt_base
```

Where:
- `collateral_i` = collateral value of asset i in base currency (USD, 8 decimals)
- `LiqThreshold_i` = liquidation threshold of asset i (basis points, e.g. 8250 = 82.50%)
- `P_shocked / P_current` = price shock ratio (e.g. 0.9 for -10% shock)
- `total_debt_base` = total debt in base currency

**Close-factor rules (Aave v3):**

Source: `LiquidationLogic.sol` in bgd-labs/aave-v3-origin

| Condition | Close Factor |
|-----------|-------------|
| `shocked_HF >= 0.95` | 50% (DEFAULT_LIQUIDATION_CLOSE_FACTOR) |
| `shocked_HF < 0.95` | 100% (MAX_LIQUIDATION_CLOSE_FACTOR) |

Liquidatable debt = `total_debt_base * close_factor(shocked_HF)`

**Shock scenarios:**
- `-5%`, `-10%`, `-20%`, `-30%` price drops
- **Asset depeg:** Only the target asset drops; correlated assets unchanged.
  Relevant for LST/LRT depegs (e.g. wstETH losing its ETH peg during validator slashing).
- **Market-wide:** All ETH-correlated assets drop together.
  Relevant for broad crypto market drawdowns.
  _Simplification: currently models as asset-depeg applied to target only;
  full multi-asset shock requires per-asset collateral decomposition (TODO)._

**E-mode:**
Users in ETH-correlated e-mode (category 1) use the e-mode liquidation threshold (9300 bps = 93%)
instead of the reserve's default threshold (e.g. 7800 bps = 78% for weETH).
This means e-mode users can sustain larger price moves before being liquidated,
but they also hold more collateral concentration in correlated assets.

## Coverage Ratio

**Formula:** `coverage_ratio = DEX_capacity_USD / liquidatable_collateral_USD`

Where:
- `DEX_capacity_USD` = max USD sellable within target price impact (2% or 5%)
- `liquidatable_collateral_USD ≈ liquidatable_debt_USD / (1 + liquidation_bonus)`

**DEX capacity estimation:**
Binary search over `UniswapV3 QuoterV2.quoteExactInputSingle` to find the largest
`amountIn` where the effective price (amountOut / amountIn) stays within the impact target.

**Limitations:**
- Simulates current pool state, not the post-shock state
- Ignores CEX liquidity (Binance, Coinbase) which is often 10-100× DEX depth
- Ignores aggregator routing (1inch, Paraswap) which can source liquidity across pools
- Multi-hop routes not modelled
- Liquidator capital constraints not modelled

## Oracle Deviation

**Formula:** `deviation_bps = (oracle_price - market_price) / market_price * 10_000`

Where:
- `oracle_price` = `AaveOracle.getAssetPrice(asset)` (USD, 8 decimals)
- `market_price` = Uniswap v3 slot0 spot price (in WETH terms, converted to USD)
- Also reported: 30-minute geometric mean TWAP from `pool.observe()`

**LST/LRT oracle note:**
wstETH and weETH oracles use exchange-rate adapters:
- wstETH: `wstETH/stETH rate × Chainlink stETH/USD`
- weETH: `weETH/eETH rate × Chainlink eETH/USD (or eETH/ETH × ETH/USD)`

This means deviation from Uniswap market price reflects the market's assessment of
the LST/LRT exchange rate vs. the oracle's static rate. A large positive deviation
(oracle above market) means the protocol over-values the collateral.

## Concentration

**Formula:**
- `top_10_pct = Σ(top-10 atoken_balance_i) / Σ(all atoken_balance_i) * 100`
- `HHI = Σ (share_i)^2` over all users (Herfindahl-Hirschman Index)

HHI interpretation:
- HHI < 0.15: unconcentrated
- 0.15–0.25: moderate concentration
- HHI > 0.25: highly concentrated

## Utilization

**Formula:**
`utilization = total_variable_debt / total_atoken_supply`

Where both values come from `AaveProtocolDataProvider.getReserveData()`.

**Cap headroom:**
`supply_cap_headroom = (supply_cap - total_supply) / supply_cap`
`borrow_cap_headroom = (borrow_cap - total_borrow) / borrow_cap`

Caps are set by Aave governance and represent the maximum total supply/borrow allowed.
Once a cap is hit, no new deposits/borrows are accepted.

## Backtest Methodology

Signals are replayed over historical position snapshots stored in `user_positions`.
The backtest does not re-fetch on-chain state; it uses the stored snapshot data.

**Limitations:**
- Signal values are computed from already-snapshotted data, not from live re-simulation.
- Snapshots taken at `SNAPSHOT_INTERVAL_HISTORY` intervals (default: daily), so
  intra-day dynamics are not captured.
- If a stress event lasted less than 24 hours, it may not appear in the backtest.

## Finality

Blocks are treated as stable once they are ≥ 12 blocks behind the current head
(`CONFIRMATIONS=12`). The Ethereum `finalized` tag (EIP-4895) would provide stronger
guarantees (2 epoch finality, ~12 minutes) but requires archive RPC support.
We use block-count confirmation as a practical approximation.

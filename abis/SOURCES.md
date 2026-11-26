# ABI Sources

All ABIs are taken from official sources.  Do not use unofficial or unverified ABIs.

| File | Source | Notes |
|------|--------|-------|
| `Pool.json` | [bgd-labs/aave-v3-origin](https://github.com/bgd-labs/aave-v3-origin/blob/main/src/core/contracts/protocol/pool/Pool.sol) verified via Etherscan for `0x87870Bca3F3fD6335C3F4ce8392D69350B4fA4E2` | Aave v3 Ethereum Core Pool |
| `PoolAddressesProvider.json` | bgd-labs/aave-v3-origin | Resolves canonical Pool address |
| `AaveOracle.json` | bgd-labs/aave-v3-origin | Price oracle |
| `AaveProtocolDataProvider.json` | bgd-labs/aave-v3-origin | Per-user/reserve data |
| `Multicall3.json` | [mds1/multicall](https://github.com/mds1/multicall/blob/main/src/Multicall3.sol) | Canonical Multicall3 |
| `UniswapV3QuoterV2.json` | [Uniswap v3-periphery](https://github.com/Uniswap/v3-periphery/blob/main/contracts/lens/QuoterV2.sol) | For DEX capacity quotes |
| `UniswapV3Pool.json` | Uniswap v3-core | For slot0 / TWAP reads |
| `ChainlinkAggregator.json` | [Chainlink contracts](https://github.com/smartcontractkit/chainlink/blob/develop/contracts/src/v0.8/interfaces/AggregatorV3Interface.sol) | Price feed reads |
| `CurvePool.json` | [Curve Finance](https://github.com/curvefi/curve-contract) | get_dy for liquidity |

## Aave v3 Ethereum Core Contract Addresses
Source: https://github.com/bgd-labs/aave-address-book/blob/main/src/AaveV3Ethereum.sol

| Contract | Address |
|----------|---------|
| Pool | `0x87870Bca3F3fD6335C3F4ce8392D69350B4fA4E2` |
| PoolAddressesProvider | `0x2f39d218133AFaB8F2B819B1066c7E434Ad94E9e` |
| AaveProtocolDataProvider | `0x7B4EB56E7CD4b454BA8ff71E4518426369a138a3` |
| AaveOracle | `0x54586bE62E3c3580375aE3723C145253060Ca0C2` |
| Multicall3 | `0xcA11bde05977b3631167028862bE2a173976CA11` |

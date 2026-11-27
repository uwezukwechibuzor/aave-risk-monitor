"""Canonical contract addresses for Aave v3 Ethereum Core market.

Source: bgd-labs/aave-address-book
https://github.com/bgd-labs/aave-address-book/blob/main/src/AaveV3Ethereum.sol

All addresses are checksummed.  The startup self-check in selfcheck.py
reads Pool from PoolAddressesProvider on-chain to verify this table.
"""

from eth_utils import to_checksum_address

# ── Aave v3 Ethereum Core ─────────────────────────────────────────────────────
POOL_ADDRESSES_PROVIDER = to_checksum_address("0x2f39d218133AFaB8F2B819B1066c7E434Ad94E9e")
POOL = to_checksum_address("0x87870Bca3F3fD6335C3F4ce8392D69350B4fA4E2")
AAVE_PROTOCOL_DATA_PROVIDER = to_checksum_address("0x7B4EB56E7CD4b454BA8ff71E4518426369a138a3")
AAVE_ORACLE = to_checksum_address("0x54586bE62E3c3580375aE3723C145253060Ca0C2")

# ── Infrastructure ────────────────────────────────────────────────────────────
# Canonical Multicall3 — same address on all EVM chains
# Source: https://github.com/mds1/multicall
MULTICALL3 = to_checksum_address("0xcA11bde05977b3631167028862bE2a173976CA11")

# Uniswap v3 QuoterV2
# Source: https://docs.uniswap.org/contracts/v3/reference/deployments
UNISWAP_V3_QUOTER_V2 = to_checksum_address("0x61fFE014bA17989E743c5F6cB21bF9697530B21e")

# WETH
WETH = to_checksum_address("0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2")

# ── SVR (Smart Value Recapture) ───────────────────────────────────────────────
# Source: bgd-labs/aave-address-book AaveV3Ethereum.SVR_STEWARD
SVR_STEWARD = to_checksum_address("0x8b493f416F5F7933cC146b1899c069F2361cad60")

# ── Asset addresses ───────────────────────────────────────────────────────────
WSTETH = to_checksum_address("0x7f39C581F595B53c5cb19bD0b3f8dA6c935E2Ca0")
WEETH = to_checksum_address("0xCd5fE23C85820F7B72D0926FC9b05b43E359b7ee")
USDC = to_checksum_address("0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48")

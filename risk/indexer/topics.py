"""Event topic hashes for all indexed contracts.

Computed with keccak256(event_signature).  Verified against on-chain data.
Values are hex strings with 0x prefix, lowercased.
"""

from eth_utils import keccak

# ── Aave v3 Pool ──────────────────────────────────────────────────────────────
# Supply(address indexed reserve, address user, address indexed onBehalfOf,
#        uint256 amount, uint16 indexed referralCode)
SUPPLY = "0x" + keccak(text="Supply(address,address,address,uint256,uint16)").hex()

# Withdraw(address indexed reserve, address indexed user, address indexed to, uint256 amount)
WITHDRAW = "0x" + keccak(text="Withdraw(address,address,address,uint256)").hex()

# Borrow(address indexed reserve, address user, address indexed onBehalfOf,
#        uint256 amount, uint8 interestRateMode, uint256 borrowRate, uint16 indexed referralCode)
BORROW = "0x" + keccak(text="Borrow(address,address,address,uint256,uint8,uint256,uint16)").hex()

# Repay(address indexed reserve, address indexed user, address indexed repayer,
#       uint256 amount, bool useATokens)
REPAY = "0x" + keccak(text="Repay(address,address,address,uint256,bool)").hex()

# LiquidationCall(address indexed collateralAsset, address indexed debtAsset,
#                 address indexed user, uint256 debtToCover,
#                 uint256 liquidatedCollateralAmount, address liquidator, bool receiveAToken)
LIQUIDATION_CALL = (
    "0x"
    + keccak(text="LiquidationCall(address,address,address,uint256,uint256,address,bool)").hex()
)

# ReserveDataUpdated(address indexed reserve, uint256 liquidityRate,
#                    uint256 stableBorrowRate, uint256 variableBorrowRate,
#                    uint256 liquidityIndex, uint256 variableBorrowIndex)
RESERVE_DATA_UPDATED = (
    "0x" + keccak(text="ReserveDataUpdated(address,uint256,uint256,uint256,uint256,uint256)").hex()
)

# ── Chainlink Aggregator ──────────────────────────────────────────────────────
# AnswerUpdated(int256 indexed current, uint256 indexed roundId, uint256 updatedAt)
ANSWER_UPDATED = "0x" + keccak(text="AnswerUpdated(int256,uint256,uint256)").hex()

# ── Uniswap v3 Pool ───────────────────────────────────────────────────────────
# Swap(address indexed sender, address indexed recipient,
#      int256 amount0, int256 amount1, uint160 sqrtPriceX96,
#      uint128 liquidity, int24 tick)
UNISWAP_SWAP = "0x" + keccak(text="Swap(address,address,int256,int256,uint160,uint128,int24)").hex()

# ── Curve pool ────────────────────────────────────────────────────────────────
# TokenExchange(address indexed buyer, int128 sold_id, uint256 tokens_sold,
#               int128 bought_id, uint256 tokens_bought)
CURVE_TOKEN_EXCHANGE = (
    "0x" + keccak(text="TokenExchange(address,int128,uint256,int128,uint256)").hex()
)

ALL_TOPICS = {
    "Supply": SUPPLY,
    "Withdraw": WITHDRAW,
    "Borrow": BORROW,
    "Repay": REPAY,
    "LiquidationCall": LIQUIDATION_CALL,
    "ReserveDataUpdated": RESERVE_DATA_UPDATED,
    "AnswerUpdated": ANSWER_UPDATED,
    "UniswapSwap": UNISWAP_SWAP,
    "CurveTokenExchange": CURVE_TOKEN_EXCHANGE,
}

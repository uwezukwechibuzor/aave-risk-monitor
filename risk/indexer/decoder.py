"""ABI decoder for indexed events.

Each function takes a raw log dict (as returned by eth_getLogs or Substreams)
and returns a typed dict of decoded fields.

Logs have:
  address, topics (list of hex), data (hex), transactionHash,
  blockNumber, logIndex
"""

from __future__ import annotations

from typing import Any

from eth_abi import decode
from eth_utils import to_checksum_address

from risk.indexer.topics import (
    ANSWER_UPDATED,
    BORROW,
    CURVE_TOKEN_EXCHANGE,
    LIQUIDATION_CALL,
    REPAY,
    RESERVE_DATA_UPDATED,
    SUPPLY,
    UNISWAP_SWAP,
    WITHDRAW,
)


def _hex_to_bytes(h: str) -> bytes:
    return bytes.fromhex(h[2:] if h.startswith("0x") else h)


def _decode_address_topic(topic: str) -> str:
    raw = bytes.fromhex(topic[2:])
    return to_checksum_address("0x" + raw[12:].hex())


def decode_supply(log: dict[str, Any]) -> dict[str, Any]:
    # Supply(address indexed reserve, address user, address indexed onBehalfOf,
    #        uint256 amount, uint16 indexed referralCode)
    reserve = _decode_address_topic(log["topics"][1])
    on_behalf_of = _decode_address_topic(log["topics"][2])
    referral_code = int(log["topics"][3], 16)
    data = _hex_to_bytes(log["data"])
    user, amount = decode(["address", "uint256"], data)
    return {
        "reserve": reserve,
        "user": to_checksum_address(user),
        "on_behalf_of": on_behalf_of,
        "amount": int(amount),
        "referral_code": referral_code,
    }


def decode_withdraw(log: dict[str, Any]) -> dict[str, Any]:
    # Withdraw(address indexed reserve, address indexed user, address indexed to, uint256 amount)
    reserve = _decode_address_topic(log["topics"][1])
    user = _decode_address_topic(log["topics"][2])
    to_addr = _decode_address_topic(log["topics"][3])
    data = _hex_to_bytes(log["data"])
    (amount,) = decode(["uint256"], data)
    return {
        "reserve": reserve,
        "user": user,
        "to_address": to_addr,
        "amount": int(amount),
    }


def decode_borrow(log: dict[str, Any]) -> dict[str, Any]:
    # Borrow(address indexed reserve, address user, address indexed onBehalfOf,
    #        uint256 amount, uint8 interestRateMode, uint256 borrowRate,
    #        uint16 indexed referralCode)
    reserve = _decode_address_topic(log["topics"][1])
    on_behalf_of = _decode_address_topic(log["topics"][2])
    referral_code = int(log["topics"][3], 16)
    data = _hex_to_bytes(log["data"])
    user, amount, rate_mode, borrow_rate = decode(["address", "uint256", "uint8", "uint256"], data)
    return {
        "reserve": reserve,
        "user": to_checksum_address(user),
        "on_behalf_of": on_behalf_of,
        "amount": int(amount),
        "borrow_rate_mode": int(rate_mode),
        "borrow_rate": int(borrow_rate),
        "referral_code": referral_code,
    }


def decode_repay(log: dict[str, Any]) -> dict[str, Any]:
    # Repay(address indexed reserve, address indexed user, address indexed repayer,
    #       uint256 amount, bool useATokens)
    reserve = _decode_address_topic(log["topics"][1])
    user = _decode_address_topic(log["topics"][2])
    repayer = _decode_address_topic(log["topics"][3])
    data = _hex_to_bytes(log["data"])
    amount, use_atokens = decode(["uint256", "bool"], data)
    return {
        "reserve": reserve,
        "user": user,
        "repayer": repayer,
        "amount": int(amount),
        "use_atokens": bool(use_atokens),
    }


def decode_liquidation_call(log: dict[str, Any]) -> dict[str, Any]:
    # LiquidationCall(address indexed collateralAsset, address indexed debtAsset,
    #                 address indexed user, uint256 debtToCover,
    #                 uint256 liquidatedCollateralAmount, address liquidator, bool receiveAToken)
    collateral = _decode_address_topic(log["topics"][1])
    debt = _decode_address_topic(log["topics"][2])
    user = _decode_address_topic(log["topics"][3])
    data = _hex_to_bytes(log["data"])
    debt_to_cover, liquidated_coll, liquidator, receive_atoken = decode(
        ["uint256", "uint256", "address", "bool"], data
    )
    return {
        "collateral_asset": collateral,
        "debt_asset": debt,
        "user": user,
        "debt_to_cover": int(debt_to_cover),
        "liquidated_collateral": int(liquidated_coll),
        "liquidator": to_checksum_address(liquidator),
        "receive_atoken": bool(receive_atoken),
    }


def decode_reserve_data_updated(log: dict[str, Any]) -> dict[str, Any]:
    # ReserveDataUpdated(address indexed reserve, uint256 liquidityRate,
    #   uint256 stableBorrowRate, uint256 variableBorrowRate,
    #   uint256 liquidityIndex, uint256 variableBorrowIndex)
    reserve = _decode_address_topic(log["topics"][1])
    data = _hex_to_bytes(log["data"])
    liq_rate, stable_rate, var_rate, liq_idx, var_idx = decode(
        ["uint256", "uint256", "uint256", "uint256", "uint256"], data
    )
    return {
        "reserve": reserve,
        "liquidity_rate": int(liq_rate),
        "stable_borrow_rate": int(stable_rate),
        "variable_borrow_rate": int(var_rate),
        "liquidity_index": int(liq_idx),
        "variable_borrow_index": int(var_idx),
    }


def decode_answer_updated(log: dict[str, Any]) -> dict[str, Any]:
    # AnswerUpdated(int256 indexed current, uint256 indexed roundId, uint256 updatedAt)
    # Note: Chainlink indexes the first two, so they're in topics[1] and topics[2]
    current = int.from_bytes(bytes.fromhex(log["topics"][1][2:]), "big", signed=True)
    round_id = int(log["topics"][2], 16)
    data = _hex_to_bytes(log["data"])
    (updated_at,) = decode(["uint256"], data)
    return {
        "answer": current,
        "round_id": round_id,
        "updated_at_ts": int(updated_at),
        "aggregator": to_checksum_address(log["address"]),
    }


def decode_uniswap_swap(log: dict[str, Any]) -> dict[str, Any]:
    # Swap(address indexed sender, address indexed recipient,
    #      int256 amount0, int256 amount1, uint160 sqrtPriceX96,
    #      uint128 liquidity, int24 tick)
    sender = _decode_address_topic(log["topics"][1])
    recipient = _decode_address_topic(log["topics"][2])
    data = _hex_to_bytes(log["data"])
    amount0, amount1, sqrt_price, _liq, tick = decode(
        ["int256", "int256", "uint160", "uint128", "int24"], data
    )
    return {
        "sender": sender,
        "recipient": recipient,
        "amount0": int(amount0),
        "amount1": int(amount1),
        "sqrt_price_x96": int(sqrt_price),
        "tick": int(tick),
    }


def decode_curve_token_exchange(log: dict[str, Any]) -> dict[str, Any]:
    # TokenExchange(address indexed buyer, int128 sold_id, uint256 tokens_sold,
    #               int128 bought_id, uint256 tokens_bought)
    buyer = _decode_address_topic(log["topics"][1])
    data = _hex_to_bytes(log["data"])
    sold_id, tokens_sold, bought_id, tokens_bought = decode(
        ["int128", "uint256", "int128", "uint256"], data
    )
    return {
        "sender": buyer,
        "recipient": buyer,
        "amount0": -int(tokens_sold),  # negative = out of pool
        "amount1": int(tokens_bought),
    }


TOPIC_DECODERS = {
    SUPPLY: decode_supply,
    WITHDRAW: decode_withdraw,
    BORROW: decode_borrow,
    REPAY: decode_repay,
    LIQUIDATION_CALL: decode_liquidation_call,
    RESERVE_DATA_UPDATED: decode_reserve_data_updated,
    ANSWER_UPDATED: decode_answer_updated,
    UNISWAP_SWAP: decode_uniswap_swap,
    CURVE_TOKEN_EXCHANGE: decode_curve_token_exchange,
}


def decode_log(log: dict[str, Any]) -> dict[str, Any] | None:
    """Decode a raw log dict.  Returns None if topic0 is not in TOPIC_DECODERS."""
    topics = log.get("topics", [])
    if not topics:
        return None
    topic0 = topics[0].lower()
    decoder = TOPIC_DECODERS.get(topic0)
    if decoder is None:
        return None
    return decoder(log)

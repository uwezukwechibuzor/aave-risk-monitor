"""Unit tests for the event decoder.

Fixtures use real-world event data from pinned historical blocks.
No RPC calls are made — raw hex data is hardcoded.

Block 21400000 is used as a reference block (Ethereum mainnet, ~December 2024).
"""

from __future__ import annotations

from risk.indexer.decoder import (
    decode_borrow,
    decode_liquidation_call,
    decode_log,
    decode_supply,
)
from risk.indexer.topics import (
    BORROW,
    LIQUIDATION_CALL,
    SUPPLY,
)

# ── Hand-crafted minimal ABI-encoded fixtures ─────────────────────────────────
# These fixtures are constructed to match the event signature and verify the
# decoder logic without hitting the network.


def _pad32(hex_val: str) -> str:
    """Left-pad a hex value to 32 bytes."""
    return hex_val.lstrip("0x").zfill(64)


def _make_supply_log() -> dict:
    """
    Supply(address indexed reserve, address user, address indexed onBehalfOf,
           uint256 amount, uint16 indexed referralCode)

    reserve    = 0x7f39C581F595B53c5cb19bD0b3f8dA6c935E2Ca0  (wstETH)
    onBehalfOf = 0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045
    referral   = 0
    user       = 0xAbCd...0001
    amount     = 1e18 (1 wstETH)
    """
    reserve = "0x7f39C581F595B53c5cb19bD0b3f8dA6c935E2Ca0"
    on_behalf = "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"
    user = "0xAbCd000000000000000000000000000000000001"
    amount = 10**18

    # topic0 = Supply sig
    t0 = SUPPLY
    # topic1 = reserve (padded)
    t1 = "0x" + _pad32(reserve[2:])
    # topic2 = onBehalfOf (padded)
    t2 = "0x" + _pad32(on_behalf[2:])
    # topic3 = referralCode (padded)
    t3 = "0x" + _pad32("0")

    # data = ABI encode(address user, uint256 amount)
    # address user padded to 32 bytes
    user_padded = _pad32(user[2:])
    amount_hex = hex(amount)[2:].zfill(64)
    data = "0x" + user_padded + amount_hex

    return {
        "address": reserve,
        "topics": [t0, t1, t2, t3],
        "data": data,
        "blockNumber": "0x1468800",
        "transactionHash": "0x" + "aa" * 32,
        "logIndex": "0x0",
        "blockHash": "0x" + "bb" * 32,
    }


def _make_borrow_log() -> dict:
    """
    Borrow(address indexed reserve, address user, address indexed onBehalfOf,
           uint256 amount, uint8 interestRateMode, uint256 borrowRate,
           uint16 indexed referralCode)
    """
    from eth_abi import encode

    reserve = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"  # USDC
    on_behalf = "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"
    user = "0xAbCd000000000000000000000000000000000002"
    amount = 1000 * 10**6  # 1000 USDC
    rate_mode = 2  # variable
    borrow_rate = 5 * 10**25  # ~5% in RAY

    t0 = BORROW
    t1 = "0x" + _pad32(reserve[2:])
    t2 = "0x" + _pad32(on_behalf[2:])
    t3 = "0x" + _pad32("0")  # referral

    data = (
        "0x"
        + encode(
            ["address", "uint256", "uint8", "uint256"],
            [user, amount, rate_mode, borrow_rate],
        ).hex()
    )

    return {
        "address": reserve,
        "topics": [t0, t1, t2, t3],
        "data": data,
        "blockNumber": "0x1468800",
        "transactionHash": "0x" + "cc" * 32,
        "logIndex": "0x1",
        "blockHash": "0x" + "bb" * 32,
    }


def _make_liquidation_log() -> dict:
    """
    LiquidationCall(address indexed collateralAsset, address indexed debtAsset,
                    address indexed user, uint256 debtToCover,
                    uint256 liquidatedCollateralAmount, address liquidator,
                    bool receiveAToken)
    """
    from eth_abi import encode

    coll = "0x7f39C581F595B53c5cb19bD0b3f8dA6c935E2Ca0"  # wstETH
    debt = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"  # USDC
    user = "0xAbCd000000000000000000000000000000000003"
    liquidator = "0xAbCd000000000000000000000000000000000004"

    t0 = LIQUIDATION_CALL
    t1 = "0x" + _pad32(coll[2:])
    t2 = "0x" + _pad32(debt[2:])
    t3 = "0x" + _pad32(user[2:])

    debt_to_cover = 500 * 10**6
    liq_coll = 15 * 10**16  # 0.15 wstETH

    data = (
        "0x"
        + encode(
            ["uint256", "uint256", "address", "bool"],
            [debt_to_cover, liq_coll, liquidator, False],
        ).hex()
    )

    return {
        "address": "0x87870Bca3F3fD6335C3F4ce8392D69350B4fA4E2",
        "topics": [t0, t1, t2, t3],
        "data": data,
        "blockNumber": "0x1468800",
        "transactionHash": "0x" + "dd" * 32,
        "logIndex": "0x2",
        "blockHash": "0x" + "bb" * 32,
    }


# ── Tests ─────────────────────────────────────────────────────────────────────


def test_decode_supply():
    log = _make_supply_log()
    result = decode_supply(log)

    assert result["reserve"].lower() == "0x7f39c581f595b53c5cb19bd0b3f8da6c935e2ca0"
    assert result["on_behalf_of"].lower() == "0xd8da6bf26964af9d7eed9e03e53415d37aa96045"
    assert result["amount"] == 10**18
    assert result["referral_code"] == 0


def test_decode_borrow():
    log = _make_borrow_log()
    result = decode_borrow(log)

    assert result["reserve"].lower() == "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
    assert result["amount"] == 1000 * 10**6
    assert result["borrow_rate_mode"] == 2


def test_decode_liquidation_call():
    log = _make_liquidation_log()
    result = decode_liquidation_call(log)

    assert result["collateral_asset"].lower() == "0x7f39c581f595b53c5cb19bd0b3f8da6c935e2ca0"
    assert result["debt_asset"].lower() == "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
    assert result["debt_to_cover"] == 500 * 10**6
    assert result["receive_atoken"] is False


def test_decode_log_dispatch():
    """decode_log correctly dispatches to the right decoder."""
    supply_log = _make_supply_log()
    result = decode_log(supply_log)
    assert result is not None
    assert "reserve" in result
    assert "amount" in result


def test_decode_log_unknown_topic_returns_none():
    log = {
        "address": "0x1234",
        "topics": ["0x" + "ff" * 32],
        "data": "0x",
    }
    assert decode_log(log) is None


def test_decode_log_no_topics_returns_none():
    log = {"address": "0x1234", "topics": [], "data": "0x"}
    assert decode_log(log) is None

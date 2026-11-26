"""Async JSON-RPC client with Multicall3 batching and rate-limit handling."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import httpx
from eth_abi import decode, encode
from eth_utils import function_signature_to_4byte_selector, to_checksum_address

from risk.addresses import MULTICALL3

logger = logging.getLogger(__name__)

ROOT = Path(__file__).parent.parent
ABI_DIR = ROOT / "abis"

RPC_CALL_COUNTER = 0  # module-level counter; reset by callers
_MAX_CALLS: int = 50_000


def _load_abi(name: str) -> list[dict[str, Any]]:
    return json.loads((ABI_DIR / f"{name}.json").read_text())


def _selector(sig: str) -> bytes:
    return function_signature_to_4byte_selector(sig)


def _encode_call(sig: str, *args: Any) -> bytes:
    """Encode a function call: 4-byte selector + ABI-encoded args."""
    sel = _selector(sig)
    # Build the types list from the signature
    arg_str = sig[sig.index("(") + 1 : sig.rindex(")")]
    types = [t.strip() for t in arg_str.split(",") if t.strip()] if arg_str else []
    if types:
        encoded = encode(types, list(args))
    else:
        encoded = b""
    return sel + encoded


class RPCBudgetExceeded(Exception):
    pass


class EthRPC:
    def __init__(self, url: str, api_key: str = "", max_calls: int = 50_000) -> None:
        self._url = url
        self._api_key = api_key
        self._max_calls = max_calls
        self._call_count = 0
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._client = httpx.AsyncClient(
            headers=headers,
            timeout=30.0,
        )

    @property
    def call_count(self) -> int:
        return self._call_count

    def _check_budget(self, increment: int = 1) -> None:
        self._call_count += increment
        if self._call_count > self._max_calls:
            raise RPCBudgetExceeded(f"RPC budget exceeded: {self._call_count} > {self._max_calls}")

    async def _rpc(self, method: str, params: list[Any], retries: int = 3) -> Any:
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        backoff = 1.0
        last_exc: Exception | None = None
        for attempt in range(retries):
            try:
                resp = await self._client.post(self._url, json=payload)
                resp.raise_for_status()
                data = resp.json()
                if "error" in data:
                    raise ValueError(f"RPC error: {data['error']}")
                return data["result"]
            except (httpx.HTTPStatusError, httpx.TimeoutException, ValueError) as e:
                last_exc = e
                if attempt < retries - 1:
                    wait = backoff * (2**attempt) + (asyncio.get_event_loop().time() % 0.5)
                    logger.warning(
                        "RPC %s attempt %d failed: %s, retrying in %.1fs",
                        method,
                        attempt + 1,
                        e,
                        wait,
                    )
                    await asyncio.sleep(wait)
        raise RuntimeError(f"RPC {method} failed after {retries} attempts: {last_exc}")

    async def eth_block_number(self) -> int:
        self._check_budget()
        result = await self._rpc("eth_blockNumber", [])
        return int(result, 16)

    async def eth_get_block_by_number(
        self, block: int | str, full_txs: bool = False
    ) -> dict[str, Any]:
        self._check_budget()
        tag = hex(block) if isinstance(block, int) else block
        return await self._rpc("eth_getBlockByNumber", [tag, full_txs])  # type: ignore[return-value]

    async def eth_call(
        self,
        to: str,
        data: str,
        block: int | str = "latest",
    ) -> str:
        self._check_budget()
        tag = hex(block) if isinstance(block, int) else block
        return await self._rpc("eth_call", [{"to": to, "data": data}, tag])  # type: ignore[return-value]

    async def eth_get_logs(
        self,
        address: str | list[str],
        topics: list[str | None],
        from_block: int,
        to_block: int,
    ) -> list[dict[str, Any]]:
        self._check_budget()
        return await self._rpc(  # type: ignore[return-value]
            "eth_getLogs",
            [
                {
                    "address": address,
                    "topics": topics,
                    "fromBlock": hex(from_block),
                    "toBlock": hex(to_block),
                }
            ],
        )

    # ── Multicall3 ─────────────────────────────────────────────────────────────

    async def multicall3(
        self,
        calls: list[tuple[str, bytes]],  # [(target, calldata), ...]
        block: int | str = "latest",
        allow_failure: bool = True,
    ) -> list[tuple[bool, bytes]]:
        """Execute multiple calls in a single eth_call via Multicall3.aggregate3."""
        self._check_budget(1)  # counts as one RPC call
        tag = hex(block) if isinstance(block, int) else block

        # Encode the aggregate3 call
        # aggregate3(Call3[] calls) where Call3 = (address target, bool allowFailure, bytes callData)
        encoded_calls = [
            (to_checksum_address(target), allow_failure, calldata) for target, calldata in calls
        ]
        calldata = encode(
            ["(address,bool,bytes)[]"],
            [encoded_calls],
        )
        selector = _selector("aggregate3((address,bool,bytes)[])")
        full_calldata = "0x" + (selector + calldata).hex()

        result_hex = await self._rpc(
            "eth_call",
            [{"to": MULTICALL3, "data": full_calldata}, tag],
        )
        # Decode result: Result[] where Result = (bool success, bytes returnData)
        raw = bytes.fromhex(result_hex[2:])
        decoded = decode(["(bool,bytes)[]"], raw)
        return [(r[0], r[1]) for r in decoded[0]]

    async def close(self) -> None:
        await self._client.aclose()


# ── High-level readers ─────────────────────────────────────────────────────────


async def get_pool_from_provider(rpc: EthRPC, provider_address: str) -> str:
    """Call PoolAddressesProvider.getPool() and return the Pool address."""
    calldata = "0x" + _selector("getPool()").hex()
    result = await rpc.eth_call(provider_address, calldata)
    # result is 32-byte ABI-encoded address
    raw = bytes.fromhex(result[2:])
    decoded = decode(["address"], raw)
    return to_checksum_address(decoded[0])


async def get_oracle_source(rpc: EthRPC, oracle: str, asset: str) -> str:
    """AaveOracle.getSourceOfAsset(asset) -> aggregator address."""
    calldata = "0x" + (_selector("getSourceOfAsset(address)") + encode(["address"], [asset])).hex()
    result = await rpc.eth_call(oracle, calldata)
    raw = bytes.fromhex(result[2:])
    decoded = decode(["address"], raw)
    return to_checksum_address(decoded[0])


async def get_asset_price(rpc: EthRPC, oracle: str, asset: str) -> int:
    """AaveOracle.getAssetPrice(asset) -> price in base currency units (8 decimals)."""
    calldata = "0x" + (_selector("getAssetPrice(address)") + encode(["address"], [asset])).hex()
    result = await rpc.eth_call(oracle, calldata)
    raw = bytes.fromhex(result[2:])
    decoded = decode(["uint256"], raw)
    return int(decoded[0])

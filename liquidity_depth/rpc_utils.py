"""Small helpers for talking to the RPC efficiently.

Robinhood Chain produces a block roughly every 100ms, so after a few months
of uptime the chain is already tens of millions of blocks tall. The public
RPC has no archive-state access (historical eth_call/eth_getCode fails) and
caps eth_getLogs both at 10,000 matched log entries and at a 10,000,000-block
span per request - get_logs_with_fallback bisects the range whenever either
cap is hit. See docs/METHODOLOGY.md ("Performance notes") for the
measurements this is based on.
"""
from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from typing import TypeVar

import requests
from eth_abi import decode as abi_decode
from eth_abi import encode as abi_encode
from web3 import Web3

from . import config

logger = logging.getLogger("liquidity_depth")

T = TypeVar("T")
R = TypeVar("R")

_LOG_LIMIT_MARKERS = (
    "exceeds limit",
    "query returned more than",
    "too many results",
    "narrow the block range",  # block-span cap ("query spans N blocks ... only 10000000 are allowed")
)
_TRANSIENT_MARKERS = ("429", "too many requests", "timeout", "timed out", "connection")
_AGGREGATE3_SELECTOR = bytes(Web3.keccak(text="aggregate3((address,bool,bytes)[])"))[:4].hex()


def get_logs_with_fallback(w3, base_params: dict, from_block: int, to_block: int) -> list:
    """eth_getLogs over [from_block, to_block], splitting the range in half
    and retrying if the node refuses because too many logs matched or the
    block span is too wide.
    """
    params = dict(base_params, fromBlock=from_block, toBlock=to_block)
    try:
        return call_with_retry(lambda: w3.eth.get_logs(params))
    except Exception as exc:  # noqa: BLE001 - RPC error shapes vary by node
        message = str(exc).lower()
        if from_block >= to_block or not any(marker in message for marker in _LOG_LIMIT_MARKERS):
            raise
        mid = (from_block + to_block) // 2
        logger.info("Log range %s-%s too large, splitting at %s", from_block, to_block, mid)
        left = get_logs_with_fallback(w3, base_params, from_block, mid)
        right = get_logs_with_fallback(w3, base_params, mid + 1, to_block)
        return left + right


def call_with_retry(fn: Callable[[], R], retries: int = 8, base_delay: float = 0.75) -> R:
    """Call a zero-arg callable, retrying with exponential backoff + jitter
    on transient errors (rate limits, timeouts, connection resets).

    The public RPC rate-limits bursts of requests, so every single RPC call
    in this project - concurrent or not - goes through this.
    """
    for attempt in range(retries):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - retrying is generic by design
            message = str(exc).lower()
            transient = any(marker in message for marker in _TRANSIENT_MARKERS)
            if not transient or attempt == retries - 1:
                raise
            delay = base_delay * (2**attempt) + random.uniform(0, base_delay)
            logger.info("Transient RPC error, retrying in %.2fs: %s", delay, exc)
            time.sleep(delay)
    raise RuntimeError("unreachable")  # pragma: no cover


def _aggregate3_call(calls: list[dict]) -> dict:
    """Pack `calls` into one Multicall3 aggregate3() eth_call, each with
    allowFailure=true so one reverting call doesn't sink the whole group.
    """
    encoded = abi_encode(
        ["(address,bool,bytes)[]"],
        [[(c["to"], True, bytes.fromhex(c["data"][2:])) for c in calls]],
    )
    return {"to": config.MULTICALL3_ADDRESS, "data": "0x" + _AGGREGATE3_SELECTOR + encoded.hex()}


def _decode_aggregate3(raw: bytes, expected: int) -> list[bytes] | None:
    """Decode aggregate3()'s (bool success, bytes returnData)[] result, with
    failed sub-calls mapped to b"" (same convention as a reverted plain
    call). Returns None if the aggregate itself failed or is malformed.
    """
    if not raw:
        return None
    try:
        decoded = abi_decode(["(bool,bytes)[]"], raw)[0]
    except Exception:  # noqa: BLE001 - any decode failure means "fall back"
        return None
    if len(decoded) != expected:
        return None
    return [bytes(data) if ok else b"" for ok, data in decoded]


def batch_eth_call(w3, calls: list[dict], batch_size: int | None = None, progress_label: str | None = None) -> list[bytes]:
    """Run many eth_calls in as few rate-limited requests as possible.

    The public RPC limits *eth_calls per second* (~35-40/s), counting each
    call inside a JSON-RPC batch separately, and answers parallel batches
    with HTTP 429 - so the only way to go faster is fewer eth_calls. Calls
    are therefore packed MULTICALL_CHUNK_SIZE at a time into Multicall3
    aggregate3() calls (one eth_call each, however many reads it does), and
    those are then sent via plain JSON-RPC batching. Measured against the
    live RPC: 415 V4 tick reads went from 12 sequential batches (~13s) to
    one ~1s request. See docs/METHODOLOGY.md "Performance notes".

    If an aggregate fails as a whole (e.g. MULTICALL3_ADDRESS pointed at a
    chain without it), that group is re-sent as plain batched calls, so
    correctness never depends on Multicall3 being there.

    `calls` is a list of {"to": address, "data": "0x..."} eth_call params.
    Returns raw return bytes per call, same order as `calls`; a call that
    reverted or was missing from the response yields b"".
    """
    if not config.MULTICALL3_ADDRESS or len(calls) <= 1:
        return _plain_batch_eth_call(w3, calls, batch_size, progress_label)

    per = config.MULTICALL_CHUNK_SIZE
    groups = [calls[i : i + per] for i in range(0, len(calls), per)]
    packed = _plain_batch_eth_call(w3, [_aggregate3_call(g) for g in groups], batch_size, progress_label)

    results: list[bytes] = []
    for group, raw in zip(groups, packed, strict=True):
        decoded = _decode_aggregate3(raw, len(group))
        if decoded is None:
            logger.warning("Multicall3 aggregate of %d calls failed; retrying them as plain batched calls", len(group))
            decoded = _plain_batch_eth_call(w3, group, batch_size, None)
        results.extend(decoded)
    return results


def _plain_batch_eth_call(w3, calls: list[dict], batch_size: int | None, progress_label: str | None) -> list[bytes]:
    """JSON-RPC batching only: RPC_BATCH_SIZE eth_calls per HTTP POST,
    sent sequentially (the RPC rejects parallel batches with HTTP 429).
    """
    batch_size = batch_size or config.RPC_BATCH_SIZE
    endpoint = w3.provider.endpoint_uri
    results: list[bytes] = [b""] * len(calls)

    for start in range(0, len(calls), batch_size):
        chunk = calls[start : start + batch_size]
        payload = [
            {"jsonrpc": "2.0", "method": "eth_call", "params": [{"to": c["to"], "data": c["data"]}, "latest"], "id": i}
            for i, c in enumerate(chunk)
        ]

        def send(payload=payload):
            resp = requests.post(endpoint, json=payload, timeout=config.RPC_TIMEOUT_SECONDS)
            resp.raise_for_status()
            return resp.json()

        response = call_with_retry(send)
        by_id = {item.get("id"): item for item in response} if isinstance(response, list) else {}
        for i in range(len(chunk)):
            hex_result = by_id.get(i, {}).get("result")
            if hex_result:
                results[start + i] = bytes.fromhex(hex_result[2:])

        if progress_label and (start // batch_size) % 10 == 0:
            done = min(start + batch_size, len(calls))
            logger.info("%s: %d/%d", progress_label, done, len(calls))

    return results


def concurrent_map(fn: Callable[[T], R], items: Iterable[T], max_workers: int | None = None) -> list[R]:
    """Run fn(item) for every item using a thread pool, preserving order.

    A thread pool (rather than asyncio) is enough here because the work is
    I/O-bound HTTP requests to the RPC endpoint, and it keeps every call
    site a plain synchronous function. Each call is retried on transient
    errors via call_with_retry.
    """
    items = list(items)
    if not items:
        return []
    workers = max_workers or config.RPC_CONCURRENCY
    with ThreadPoolExecutor(max_workers=min(workers, len(items))) as pool:
        return list(pool.map(lambda item: call_with_retry(lambda: fn(item)), items))

"""ERC20 metadata lookups and USD price resolution for quote tokens.

USD pricing only needs to cover the small, fixed set of *quote* tokens a
pool can be paired against (WETH, USDG, NVDA, ...) - the target token's own
USD price is derived per-pool from its spot price against whichever quote
token it is paired with, so it never needs an external price feed itself.
"""
from __future__ import annotations

import logging
import time

import requests
from web3 import Web3

from . import config
from .models import TokenMeta
from .rpc_utils import batch_eth_call

logger = logging.getLogger("liquidity_depth")

_metadata_cache: dict[str, TokenMeta] = {}
# address -> (fetched_at, price). Failures are cached as None too, so every
# pool in one run sees the same answer instead of a flaky feed pricing some
# pools and skipping others. Entries expire after _PRICE_TTL_SECONDS so a
# long-running web server doesn't keep serving the first price it saw.
_price_cache: dict[str, tuple[float, float | None]] = {}
_PRICE_TTL_SECONDS = 60
_PRICE_FETCH_ATTEMPTS = 3

NATIVE_ETH_META = TokenMeta(address=config.NATIVE_ETH_SENTINEL, symbol="ETH", name="Native ETH", decimals=18)

_NAME_SELECTOR = bytes(Web3.keccak(text="name()"))[:4].hex()
_SYMBOL_SELECTOR = bytes(Web3.keccak(text="symbol()"))[:4].hex()
_DECIMALS_SELECTOR = bytes(Web3.keccak(text="decimals()"))[:4].hex()


def _decode_string_return(raw: bytes) -> str | None:
    try:
        if len(raw) < 64:
            return None
        length = int.from_bytes(raw[32:64], "big")
        return raw[64 : 64 + length].decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return None


def get_token_metas(w3: Web3, addresses: list[str]) -> dict[str, TokenMeta]:
    """Batched version of get_token_meta for many tokens at once (used
    after V4 discovery, where dozens of distinct quote tokens can show up
    across the surviving candidate pools).
    """
    addresses = [Web3.to_checksum_address(a) for a in addresses]
    result: dict[str, TokenMeta] = {}
    to_fetch: list[str] = []
    for a in addresses:
        if a == config.NATIVE_ETH_SENTINEL:
            result[a] = NATIVE_ETH_META
        elif a in _metadata_cache:
            result[a] = _metadata_cache[a]
        elif a not in to_fetch:
            to_fetch.append(a)

    if not to_fetch:
        return result

    calls = []
    for a in to_fetch:
        calls.append({"to": a, "data": "0x" + _NAME_SELECTOR})
        calls.append({"to": a, "data": "0x" + _SYMBOL_SELECTOR})
        calls.append({"to": a, "data": "0x" + _DECIMALS_SELECTOR})
    raw = batch_eth_call(w3, calls)

    for i, a in enumerate(to_fetch):
        name_raw, symbol_raw, decimals_raw = raw[3 * i], raw[3 * i + 1], raw[3 * i + 2]
        name = _decode_string_return(name_raw) or a
        symbol = _decode_string_return(symbol_raw) or a[:6]
        decimals = int.from_bytes(decimals_raw, "big") if decimals_raw else 18
        meta = TokenMeta(address=a, symbol=symbol, name=name, decimals=decimals)
        _metadata_cache[a] = meta
        result[a] = meta

    return result


def get_token_meta(w3: Web3, address: str) -> TokenMeta:
    """Single-token convenience wrapper around get_token_metas.

    This used to make 3 separate, unbatched eth_calls (name/symbol/decimals)
    via web3.py's high-level contract interface - each one a full HTTP round
    trip. Profiling against the live RPC showed that alone taking ~30s for
    one token (individual, non-batched calls plateau at ~8-9/s and are far
    more prone to needing a retry - see docs/METHODOLOGY.md "Performance notes"), against
    a fraction of a second once routed through the same batch_eth_call path
    get_token_metas already used for every *other* token in the pipeline.
    """
    address = Web3.to_checksum_address(address)
    if address == config.NATIVE_ETH_SENTINEL:
        return NATIVE_ETH_META
    return get_token_metas(w3, [address])[address]


def _fetch_eth_usd_price() -> float:
    resp = requests.get(
        "https://api.coingecko.com/api/v3/simple/price",
        params={"ids": "ethereum", "vs_currencies": "usd"},
        timeout=10,
    )
    resp.raise_for_status()
    return float(resp.json()["ethereum"]["usd"])


def _fetch_nvda_usd_price() -> float:
    resp = requests.get(
        "https://query1.finance.yahoo.com/v8/finance/chart/NVDA",
        params={"interval": "1d", "range": "1d"},
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=10,
    )
    resp.raise_for_status()
    meta = resp.json()["chart"]["result"][0]["meta"]
    return float(meta["regularMarketPrice"])


# Stablecoins assumed pegged to $1. This is a simplification - see the
# docs/METHODOLOGY.md ("Limitations") for why this can be wrong during a depeg.
_STABLECOIN_PRICES = {
    config.USDG: 1.0,
}

# External price feeds for non-stablecoin quote tokens. Each is retried a
# few times; if it still fails, pools quoted in that token are skipped from
# USD aggregation (but still listed in the raw pool table).
_EXTERNAL_PRICE_FEEDS = {
    config.WETH: _fetch_eth_usd_price,
    config.NATIVE_ETH_SENTINEL: _fetch_eth_usd_price,
    config.NVDA: _fetch_nvda_usd_price,
}


def get_quote_token_usd_price(address: str) -> float | None:
    """Best-effort USD price for a quote token. Returns None if unknown."""
    address = Web3.to_checksum_address(address) if address != config.NATIVE_ETH_SENTINEL else address
    if address in _STABLECOIN_PRICES:
        return _STABLECOIN_PRICES[address]
    if address not in _EXTERNAL_PRICE_FEEDS:
        return None

    cached = _price_cache.get(address)
    if cached is not None and time.time() - cached[0] < _PRICE_TTL_SECONDS:
        return cached[1]

    price = None
    for attempt in range(_PRICE_FETCH_ATTEMPTS):
        try:
            price = _EXTERNAL_PRICE_FEEDS[address]()
            break
        except Exception as exc:  # noqa: BLE001 - external API can be flaky
            if attempt == _PRICE_FETCH_ATTEMPTS - 1:
                logger.warning("Could not fetch USD price for %s: %s", address, exc)
            else:
                time.sleep(0.5 * 2**attempt)

    _price_cache[address] = (time.time(), price)
    return price

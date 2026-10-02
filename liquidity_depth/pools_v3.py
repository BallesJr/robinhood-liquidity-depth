"""Uniswap V3 pool discovery and state/tick reading on Robinhood Chain."""
from __future__ import annotations

import logging

from web3 import Web3

from . import config, liquidity_math, tick_bitmap
from .models import PoolInfo
from .rpc_utils import batch_eth_call
from .tokens import get_quote_token_usd_price, get_token_metas

logger = logging.getLogger("liquidity_depth")

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"

_TOKEN0_SELECTOR = bytes(Web3.keccak(text="token0()"))[:4].hex()
_TOKEN1_SELECTOR = bytes(Web3.keccak(text="token1()"))[:4].hex()
_FEE_SELECTOR = bytes(Web3.keccak(text="fee()"))[:4].hex()
_TICK_SPACING_SELECTOR = bytes(Web3.keccak(text="tickSpacing()"))[:4].hex()
_LIQUIDITY_SELECTOR = bytes(Web3.keccak(text="liquidity()"))[:4].hex()
_SLOT0_SELECTOR = bytes(Web3.keccak(text="slot0()"))[:4].hex()
_TICK_BITMAP_SELECTOR = bytes(Web3.keccak(text="tickBitmap(int16)"))[:4].hex()
_TICKS_SELECTOR = bytes(Web3.keccak(text="ticks(int24)"))[:4].hex()
_BALANCE_OF_SELECTOR = bytes(Web3.keccak(text="balanceOf(address)"))[:4].hex()
_GET_POOL_SELECTOR = bytes(Web3.keccak(text="getPool(address,address,uint24)"))[:4].hex()


def _balance_of_calldata(address: str) -> str:
    return "0x" + _BALANCE_OF_SELECTOR + "0" * 24 + address.lower().replace("0x", "")


def _get_pool_calldata(token_a: str, token_b: str, fee: int) -> str:
    a = token_a.lower().replace("0x", "").rjust(64, "0")
    b = token_b.lower().replace("0x", "").rjust(64, "0")
    f = fee.to_bytes(32, "big").hex()
    return "0x" + _GET_POOL_SELECTOR + a + b + f


def _tick_bitmap_calldata(word_pos: int) -> str:
    return "0x" + _TICK_BITMAP_SELECTOR + word_pos.to_bytes(32, "big", signed=True).hex()


def _ticks_calldata(tick: int) -> str:
    return "0x" + _TICKS_SELECTOR + tick.to_bytes(32, "big", signed=True).hex()


def _decode_signed_word(word: bytes) -> int:
    value = int.from_bytes(word, "big")
    return value - (1 << 256) if value & (1 << 255) else value


def _decode_address(word: bytes) -> str:
    return Web3.to_checksum_address("0x" + word.hex()[-40:])


def discover_v3_pools(w3: Web3, token_address: str) -> list[PoolInfo]:
    """Find every V3 pool pairing `token_address` with a configured quote
    token, across the standard fee tiers, using the factory's getPool()
    getter directly (fast: N_quotes x 4 view calls, no log scanning needed).

    The lookups are batched into one round trip via batch_eth_call rather
    than N individual (even if concurrent) eth_calls - profiling showed
    individual calls plateau at ~8-9/s and are far more prone to needing a
    retry (see docs/METHODOLOGY.md "Performance notes"), so this alone cut V3 discovery
    from ~9s to under a second in testing.
    """
    token_address = Web3.to_checksum_address(token_address)

    candidates = [
        (quote, fee)
        for quote in config.QUOTE_TOKENS
        if quote.lower() != token_address.lower()
        for fee in config.STANDARD_V3_FEE_TIERS
    ]

    calls = [{"to": config.UNISWAP_V3_FACTORY, "data": _get_pool_calldata(token_address, quote, fee)} for quote, fee in candidates]
    raw = batch_eth_call(w3, calls)

    found = []
    for (quote, fee), r in zip(candidates, raw):
        if len(r) < 32:
            continue
        addr = _decode_address(r)
        if addr.lower() != ZERO_ADDRESS:
            found.append((quote, fee, addr))
    if not found:
        return []

    pools = _read_pools_state(w3, [addr for _, _, addr in found])
    for (quote, fee, addr), _ in zip(found, pools):
        logger.info("Found V3 pool %s (%s, fee=%s)", addr, quote, fee)
    return pools


def _read_pools_state(w3: Web3, pool_addresses: list[str]) -> list[PoolInfo]:
    """Batch-read token0/token1/fee/tickSpacing/liquidity/slot0 for every
    pool in one (or a couple of) HTTP round trips via batch_eth_call,
    instead of six sequential calls per pool.
    """
    calls = []
    for addr in pool_addresses:
        calls.extend(
            {"to": addr, "data": "0x" + selector}
            for selector in (
                _TOKEN0_SELECTOR,
                _TOKEN1_SELECTOR,
                _FEE_SELECTOR,
                _TICK_SPACING_SELECTOR,
                _LIQUIDITY_SELECTOR,
                _SLOT0_SELECTOR,
            )
        )

    raw = batch_eth_call(w3, calls)

    parsed = []
    token_addresses: set[str] = set()
    for i, addr in enumerate(pool_addresses):
        base = i * 6
        token0_addr = _decode_address(raw[base])
        token1_addr = _decode_address(raw[base + 1])
        fee = int.from_bytes(raw[base + 2], "big")
        tick_spacing = _decode_signed_word(raw[base + 3])
        liquidity = int.from_bytes(raw[base + 4], "big")
        slot0_raw = raw[base + 5]
        sqrt_price_x96 = int.from_bytes(slot0_raw[0:32], "big")
        tick = _decode_signed_word(slot0_raw[32:64])
        parsed.append((addr, token0_addr, token1_addr, fee, tick_spacing, liquidity, sqrt_price_x96, tick))
        token_addresses.update((token0_addr, token1_addr))

    token_metas = get_token_metas(w3, list(token_addresses))

    return [
        PoolInfo(
            version="v3",
            pool_key=addr,
            fee=fee,
            tick_spacing=tick_spacing,
            hooks=None,
            token0=token_metas[token0_addr],
            token1=token_metas[token1_addr],
            sqrt_price_x96=sqrt_price_x96,
            tick=tick,
            liquidity=liquidity,
            tvl_usd=None,  # computed later, in refresh_state - see its docstring
        )
        for addr, token0_addr, token1_addr, fee, tick_spacing, liquidity, sqrt_price_x96, tick in parsed
    ]


def _tvl_from_balances(pool_info: PoolInfo, target_address: str, bal0: int, bal1: int) -> float | None:
    """Shared valuation step between compute_tvl_usd (single pool) and
    refresh_states_batch (many pools) - given reserve balances already
    read, price them the same way aggregation.py values liquidity segments,
    so TVL and "liquidity in window" stay directly comparable.
    """
    token0, token1 = pool_info.token0, pool_info.token1
    target_is_token0 = token0.address.lower() == target_address.lower()
    quote_token = token1 if target_is_token0 else token0
    quote_price = get_quote_token_usd_price(quote_token.address)

    price0 = price1 = None
    if quote_price is not None:
        human = liquidity_math.human_price_from_sqrt_price_x96(pool_info.sqrt_price_x96, token0.decimals, token1.decimals)
        target_price = human * quote_price if target_is_token0 else quote_price / human
        price0 = target_price if target_is_token0 else quote_price
        price1 = quote_price if target_is_token0 else target_price

    total = 0.0
    known = False
    if price0 is not None:
        total += (bal0 / 10**token0.decimals) * price0
        known = True
    if price1 is not None:
        total += (bal1 / 10**token1.decimals) * price1
        known = True
    return total if known else None


def compute_tvl_usd(w3: Web3, pool_info: PoolInfo, target_address: str) -> float | None:
    """Real on-chain TVL for a V3 pool: it holds its own reserves directly,
    unlike V4's shared-singleton PoolManager, so this is just two
    balanceOf() calls. The target token itself has no external USD price
    (see tokens.py), so its leg is priced using the pool's own spot price
    against whichever side *does* have one - the same derivation
    aggregation.py uses for the depth chart, so this TVL and "liquidity in
    window" are directly comparable in the CLI/web output.
    """
    token0, token1 = pool_info.token0, pool_info.token1
    try:
        raw = batch_eth_call(
            w3,
            [
                {"to": token0.address, "data": _balance_of_calldata(pool_info.pool_key)},
                {"to": token1.address, "data": _balance_of_calldata(pool_info.pool_key)},
            ],
        )
        bal0 = int.from_bytes(raw[0], "big")
        bal1 = int.from_bytes(raw[1], "big")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not read reserves for pool %s: %s", pool_info.pool_key, exc)
        return None
    return _tvl_from_balances(pool_info, target_address, bal0, bal1)


def refresh_state(w3: Web3, pool_info: PoolInfo, target_address: str) -> None:
    """Re-read slot0()/liquidity()/reserves immediately before fetching
    ticks, rather than relying on the snapshot taken at discovery time.

    Pool discovery and tick fetching can be minutes apart in the same run
    (V4 discovery alone can take a while - see docs/METHODOLOGY.md "Performance
    notes"), and this chain produces a block every ~100ms with meme-pool
    volumes that move fast. Reusing a stale liquidity()/tick snapshot
    together with freshly-fetched ticks would reconstruct a state that
    never actually existed; refreshing everything together right before
    the tick fetch keeps the whole per-pool calculation internally
    consistent.

    Single-pool convenience wrapper around refresh_states_batch - see that
    docstring for why aggregation.py calls the batch version directly
    instead of this in a loop.
    """
    refresh_states_batch(w3, [pool_info], target_address)


def refresh_states_batch(w3: Web3, pools: list[PoolInfo], target_address: str) -> None:
    """Batch version of refresh_state for many pools at once.

    refresh_state alone did 2 HTTP round trips per pool (slot0+liquidity,
    then 2x balanceOf() for TVL); called in a loop across ~20 pools that
    was ~40 round trips. Profiling against the live RPC showed round-trip
    latency, not per-call throughput, is what actually dominates cost here
    (a batch of 4 calls and a batch of 40 calls cost about the same wall
    time) - so batching every pool's calls into one shot brings the whole
    refresh phase down to 1-2 round trips total, regardless of pool count.
    Mutates each PoolInfo in place, same as refresh_state.
    """
    if not pools:
        return
    calls = []
    for p in pools:
        calls.append({"to": p.pool_key, "data": "0x" + _SLOT0_SELECTOR})
        calls.append({"to": p.pool_key, "data": "0x" + _LIQUIDITY_SELECTOR})
        calls.append({"to": p.token0.address, "data": _balance_of_calldata(p.pool_key)})
        calls.append({"to": p.token1.address, "data": _balance_of_calldata(p.pool_key)})

    raw = batch_eth_call(w3, calls)

    for i, p in enumerate(pools):
        base = i * 4
        slot0_raw, liquidity_raw, bal0_raw, bal1_raw = raw[base], raw[base + 1], raw[base + 2], raw[base + 3]
        p.sqrt_price_x96 = int.from_bytes(slot0_raw[0:32], "big")
        p.tick = _decode_signed_word(slot0_raw[32:64])
        p.liquidity = int.from_bytes(liquidity_raw, "big")
        p.tvl_usd = _tvl_from_balances(p, target_address, int.from_bytes(bal0_raw, "big"), int.from_bytes(bal1_raw, "big"))


def fetch_initialized_ticks(w3: Web3, pool_info: PoolInfo, tick_lo: int, tick_hi: int) -> list[tuple[int, int]]:
    """Fetch (tick, liquidityNet) for every initialized tick in [tick_lo,
    tick_hi], via direct tickBitmap()/ticks() calls on the pool contract -
    no dependency on a TickLens helper contract.

    Single-pool convenience wrapper around fetch_initialized_ticks_batch -
    see that docstring for why aggregation.py calls the batch version
    directly instead of this in a loop.
    """
    return fetch_initialized_ticks_batch(w3, [(pool_info, tick_lo, tick_hi)]).get(pool_info.pool_key, [])


def fetch_initialized_ticks_batch(
    w3: Web3, pools_with_ranges: list[tuple[PoolInfo, int, int]]
) -> dict[str, list[tuple[int, int]]]:
    """Batch version of fetch_initialized_ticks for many pools at once -
    same round-trip-count reasoning as refresh_states_batch: this collapses
    what used to be 2 HTTP round trips *per pool* (bitmap words, then
    ticks) into 2 round trips *total*. Returns {pool_key: [(tick,
    liquidityNet), ...]}.
    """
    if not pools_with_ranges:
        return {}

    per_pool_words: list[list[int]] = []
    word_calls = []
    for pool, tick_lo, tick_hi in pools_with_ranges:
        words = list(tick_bitmap.word_range_for_ticks(tick_lo, tick_hi, pool.tick_spacing))
        per_pool_words.append(words)
        word_calls.extend({"to": pool.pool_key, "data": _tick_bitmap_calldata(w)} for w in words)

    word_raw = batch_eth_call(w3, word_calls)

    per_pool_candidates: list[list[int]] = []
    idx = 0
    for (pool, tick_lo, tick_hi), words in zip(pools_with_ranges, per_pool_words):
        candidates = []
        for word_pos in words:
            raw = word_raw[idx]
            idx += 1
            bitmap = int.from_bytes(raw, "big") if raw else 0
            for t in tick_bitmap.initialized_ticks_in_word(word_pos, bitmap, pool.tick_spacing):
                if tick_lo <= t <= tick_hi:
                    candidates.append(t)
        per_pool_candidates.append(candidates)

    tick_calls = []
    for (pool, _, _), candidates in zip(pools_with_ranges, per_pool_candidates):
        tick_calls.extend({"to": pool.pool_key, "data": _ticks_calldata(t)} for t in candidates)

    tick_raw = batch_eth_call(w3, tick_calls)

    result: dict[str, list[tuple[int, int]]] = {}
    idx = 0
    for (pool, _, _), candidates in zip(pools_with_ranges, per_pool_candidates):
        entries = []
        for t in candidates:
            raw = tick_raw[idx]
            idx += 1
            liquidity_net = _decode_signed_word(raw[32:64]) if len(raw) >= 64 else 0
            entries.append((t, liquidity_net))
        result[pool.pool_key] = entries
    return result

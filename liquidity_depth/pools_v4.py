"""Uniswap V4 pool discovery and state/tick reading on Robinhood Chain.

V4 has no per-pool contract and no getPool()-style getter: every pool lives
inside the PoolManager singleton, keyed by a PoolId that includes an
arbitrary hooks address we cannot guess. So pools are discovered from the
PoolManager's `Initialize` events instead (mirrors what a subgraph would
index), and all further state (slot0, liquidity, ticks, tick bitmap) is
read directly from PoolManager storage via `extsload`, using the exact slot
layout from Uniswap's own StateLibrary.sol / Pool.sol / Slot0.sol - this
avoids depending on the correctness of a separately-deployed StateView
helper contract's address; the only address this module trusts is the
already-verified PoolManager itself.

A popular token can have thousands of Initialize events on the shared
PoolManager (most from dust/probe pools). Two things keep that manageable:
1. The candidate list (which PoolIds exist) is cached on disk per token
   (pool_cache.py) and only extended with blocks scanned since last time -
   Initialize events are append-only, so this is always safe.
2. Candidates quoted in a token this tool can actually price in USD are
   liquidity-checked first; if that alone already fills MAX_V4_POOLS, the
   (often much larger) tail of unpriceable spam-token candidates is never
   checked at all.
Whatever liquidity checks do run go through rpc_utils.batch_eth_call
(JSON-RPC batching) rather than one HTTP round-trip per call - see
docs/METHODOLOGY.md "Performance notes".
"""
from __future__ import annotations

import logging

from web3 import Web3

from . import config, pool_cache, tick_bitmap
from .models import PoolInfo
from .rpc_utils import batch_eth_call, call_with_retry, get_logs_with_fallback
from .tokens import get_token_metas

logger = logging.getLogger("liquidity_depth")

# --- Pool.State storage layout (Uniswap v4-core: PoolManager.sol / Pool.sol) --
POOLS_SLOT = 6  # `mapping(PoolId => Pool.State) pools` is slot 6 of PoolManager
LIQUIDITY_OFFSET = 3  # Pool.State.liquidity
TICKS_OFFSET = 4  # Pool.State.ticks mapping
TICK_BITMAP_OFFSET = 5  # Pool.State.tickBitmap mapping

_KNOWN_QUOTE_TOKENS = {*config.QUOTE_TOKENS, config.NATIVE_ETH_SENTINEL}

_INIT_EVENT_SIGNATURE = "Initialize(bytes32,address,address,uint24,int24,address,uint160,int24)"
_EXTSLOAD_SELECTOR = bytes(Web3.keccak(text="extsload(bytes32)"))[:4].hex()


def _init_event_topic() -> str:
    return Web3.to_hex(Web3.keccak(text=_INIT_EVENT_SIGNATURE))


def _address_topic(address: str) -> str:
    return "0x" + "0" * 24 + address.lower().replace("0x", "")


def _int256_bytes(value: int) -> bytes:
    return value.to_bytes(32, "big", signed=True)


def _pool_state_slot(pool_id: bytes) -> int:
    packed = pool_id + POOLS_SLOT.to_bytes(32, "big")
    return int.from_bytes(Web3.keccak(packed), "big")


def _tick_info_slot(pool_id: bytes, tick: int) -> int:
    ticks_mapping_slot = (_pool_state_slot(pool_id) + TICKS_OFFSET) % (2**256)
    packed = _int256_bytes(tick) + ticks_mapping_slot.to_bytes(32, "big")
    return int.from_bytes(Web3.keccak(packed), "big")


def _tick_bitmap_slot(pool_id: bytes, word_pos: int) -> int:
    bitmap_mapping_slot = (_pool_state_slot(pool_id) + TICK_BITMAP_OFFSET) % (2**256)
    packed = _int256_bytes(word_pos) + bitmap_mapping_slot.to_bytes(32, "big")
    return int.from_bytes(Web3.keccak(packed), "big")


def _parse_slot0(word: int) -> tuple[int, int]:
    sqrt_price_x96 = word & ((1 << 160) - 1)
    tick_raw = (word >> 160) & 0xFFFFFF
    tick = tick_raw - (1 << 24) if tick_raw & (1 << 23) else tick_raw
    return sqrt_price_x96, tick


def _parse_tick_info_word(word: int) -> tuple[int, int]:
    liquidity_gross = word & ((1 << 128) - 1)
    liquidity_net_raw = (word >> 128) & ((1 << 128) - 1)
    liquidity_net = liquidity_net_raw - (1 << 128) if liquidity_net_raw & (1 << 127) else liquidity_net_raw
    return liquidity_gross, liquidity_net


def _extsload_calldata(slot_int: int) -> str:
    slot_bytes = (slot_int % (2**256)).to_bytes(32, "big")
    return "0x" + _EXTSLOAD_SELECTOR + slot_bytes.hex()


def _extsload_call(slot_int: int) -> dict:
    return {"to": config.UNISWAP_V4_POOL_MANAGER, "data": _extsload_calldata(slot_int)}


def _word_to_int(raw: bytes) -> int:
    return int.from_bytes(raw, "big") if raw else 0


def _decode_initialize_log(log) -> dict:
    topics = log["topics"]
    pool_id = Web3.to_hex(topics[1])
    currency0 = Web3.to_checksum_address("0x" + Web3.to_hex(topics[2])[-40:])
    currency1 = Web3.to_checksum_address("0x" + Web3.to_hex(topics[3])[-40:])
    data = bytes(log["data"])
    fee = int.from_bytes(data[0:32], "big")
    tick_spacing_raw = int.from_bytes(data[32:64], "big")
    tick_spacing = tick_spacing_raw - (1 << 24) if tick_spacing_raw & (1 << 23) else tick_spacing_raw
    hooks = Web3.to_checksum_address("0x" + Web3.to_hex(data[64:96])[-40:])
    return {
        "pool_id": pool_id,
        "currency0": currency0,
        "currency1": currency1,
        "fee": fee,
        "tick_spacing": tick_spacing,
        "hooks": hooks,
    }


def _check_liquidity(w3: Web3, items: list[tuple[str, dict]], progress_label: str) -> list[tuple]:
    """Batch-read slot0+liquidity for a set of (pool_id, info) candidates,
    returning only the ones with nonzero in-range liquidity as
    (pool_id_hex, info, sqrt_price_x96, tick, liquidity) tuples.
    """
    if not items:
        return []
    calls = []
    for pool_id_hex, _ in items:
        state_slot = _pool_state_slot(bytes.fromhex(pool_id_hex[2:]))
        calls.append(_extsload_call(state_slot))
        calls.append(_extsload_call(state_slot + LIQUIDITY_OFFSET))

    raw_results = batch_eth_call(w3, calls, progress_label=progress_label)

    alive = []
    for i, (pool_id_hex, info) in enumerate(items):
        sqrt_price_x96, tick = _parse_slot0(_word_to_int(raw_results[2 * i]))
        liquidity = _word_to_int(raw_results[2 * i + 1]) & ((1 << 128) - 1)
        if liquidity > 0:
            alive.append((pool_id_hex, info, sqrt_price_x96, tick, liquidity))
    return alive


def discover_v4_pools(w3: Web3, token_address: str, use_cache: bool = True) -> list[PoolInfo]:
    token_address = Web3.to_checksum_address(token_address)
    init_topic = _init_event_topic()
    token_topic = _address_topic(token_address)
    latest = call_with_retry(lambda: w3.eth.block_number)

    cached = pool_cache.load(token_address) if use_cache else None
    candidates: dict[str, dict] = dict(cached["candidates"]) if cached else {}
    from_block = cached["last_block"] + 1 if cached else 0

    if from_block <= latest:
        # Initialize's indexed topics are [signature, id, currency0,
        # currency1] - the PoolId comes first, so currency0 is topic 2, not
        # topic 1. Getting this off by one silently drops every pool where
        # the token is currency1, i.e. every pair with a lower-addressed
        # token such as native ETH (0x0) or USDG.
        logs_as_currency0 = get_logs_with_fallback(
            w3, {"address": config.UNISWAP_V4_POOL_MANAGER, "topics": [init_topic, None, token_topic]}, from_block, latest
        )
        logs_as_currency1 = get_logs_with_fallback(
            w3, {"address": config.UNISWAP_V4_POOL_MANAGER, "topics": [init_topic, None, None, token_topic]}, from_block, latest
        )
        new_count = 0
        for log in [*logs_as_currency0, *logs_as_currency1]:
            decoded = _decode_initialize_log(log)
            new_count += decoded["pool_id"] not in candidates
            candidates[decoded["pool_id"]] = decoded
        if cached:
            logger.info(
                "Pool cache hit for %s: %d cached candidates + %d new since block %d",
                token_address, len(cached["candidates"]), new_count, cached["last_block"],
            )

    if use_cache:
        pool_cache.save(token_address, latest, candidates)

    if not candidates:
        return []

    logger.info("Found %d candidate V4 pools for %s; checking current liquidity...", len(candidates), token_address)

    def is_known_quote(item) -> bool:
        info = item[1]
        quote = info["currency1"] if info["currency0"].lower() == token_address.lower() else info["currency0"]
        return quote in _KNOWN_QUOTE_TOKENS

    # currency0/currency1 come straight from the (cached) Initialize event,
    # for free - so pools quoted in a token we can actually price in USD
    # can be identified, and checked, *before* touching the (often huge)
    # tail of one-off spam-token pools nobody can price anyway. If that
    # already gives enough alive pools to fill MAX_V4_POOLS, the expensive
    # full scan is skipped entirely.
    known_items = [c for c in candidates.items() if is_known_quote(c)]
    other_items = [c for c in candidates.items() if not is_known_quote(c)]

    alive_known = _check_liquidity(w3, known_items, "Checking known-quote V4 pools")

    if len(alive_known) >= config.MAX_V4_POOLS:
        alive_other: list[tuple] = []
        logger.info(
            "%d known-quote V4 candidates already cover MAX_V4_POOLS; skipping liquidity check on %d other candidates",
            len(known_items), len(other_items),
        )
    else:
        alive_other = _check_liquidity(w3, other_items, "Checking other V4 candidate liquidity")

    known_quote_pools = sorted(alive_known, key=lambda c: c[4], reverse=True)
    other_pools = sorted(alive_other, key=lambda c: c[4], reverse=True)
    kept = (known_quote_pools + other_pools)[: config.MAX_V4_POOLS]

    logger.info(
        "%d/%d candidate V4 pools have nonzero in-range liquidity (%d quoted in a known token); keeping top %d",
        len(known_quote_pools) + len(other_pools),
        len(candidates),
        len(known_quote_pools),
        len(kept),
    )

    token_addresses = {info["currency0"] for _, info, *_ in kept} | {info["currency1"] for _, info, *_ in kept}
    token_metas = get_token_metas(w3, list(token_addresses))

    pools = []
    for pool_id_hex, info, sqrt_price_x96, tick, liquidity in kept:
        token0 = token_metas[info["currency0"]]
        token1 = token_metas[info["currency1"]]
        pools.append(
            PoolInfo(
                version="v4",
                pool_key=pool_id_hex,
                fee=info["fee"],
                tick_spacing=info["tick_spacing"],
                hooks=info["hooks"],
                token0=token0,
                token1=token1,
                sqrt_price_x96=sqrt_price_x96,
                tick=tick,
                liquidity=liquidity,
                tvl_usd=None,  # PoolManager pools reserves across all pools; see docs/METHODOLOGY.md "Limitations"
            )
        )
    return pools


def refresh_state(w3: Web3, pool_info: PoolInfo, target_address: str) -> None:
    """Re-read slot0/liquidity immediately before fetching ticks - see the
    identical concern documented in pools_v3.refresh_state. `target_address`
    is accepted only so callers can dispatch to V3/V4 uniformly; V4 pools
    don't get a TVL figure (see PoolInfo.tvl_usd / docs/METHODOLOGY.md "Limitations").

    Single-pool convenience wrapper around refresh_states_batch - see that
    docstring for why aggregation.py calls the batch version directly
    instead of this in a loop.
    """
    refresh_states_batch(w3, [pool_info])


def refresh_states_batch(w3: Web3, pools: list[PoolInfo]) -> None:
    """Batch version of refresh_state for many pools at once - see
    pools_v3.refresh_states_batch's docstring for the round-trip-count
    reasoning (round-trip latency, not per-call throughput, dominates cost
    on this RPC). V4 pools don't get a TVL figure either way. Mutates each
    PoolInfo in place.
    """
    if not pools:
        return
    calls = []
    for p in pools:
        pool_id_bytes = bytes.fromhex(p.pool_key[2:])
        state_slot = _pool_state_slot(pool_id_bytes)
        calls.append(_extsload_call(state_slot))
        calls.append(_extsload_call(state_slot + LIQUIDITY_OFFSET))

    raw = batch_eth_call(w3, calls)
    for i, p in enumerate(pools):
        sqrt_price_x96, tick = _parse_slot0(_word_to_int(raw[2 * i]))
        p.sqrt_price_x96, p.tick = sqrt_price_x96, tick
        p.liquidity = _word_to_int(raw[2 * i + 1]) & ((1 << 128) - 1)


def fetch_initialized_ticks(w3: Web3, pool_info: PoolInfo, tick_lo: int, tick_hi: int) -> list[tuple[int, int]]:
    """Fetch (tick, liquidityNet) for every initialized tick in [tick_lo,
    tick_hi] by reading raw PoolManager storage via extsload, batched.

    Single-pool convenience wrapper around fetch_initialized_ticks_batch -
    see that docstring for why aggregation.py calls the batch version
    directly instead of this in a loop.
    """
    return fetch_initialized_ticks_batch(w3, [(pool_info, tick_lo, tick_hi)]).get(pool_info.pool_key, [])


def fetch_initialized_ticks_batch(
    w3: Web3, pools_with_ranges: list[tuple[PoolInfo, int, int]]
) -> dict[str, list[tuple[int, int]]]:
    """Batch version of fetch_initialized_ticks for many pools at once -
    see pools_v3's twin for the round-trip-count reasoning. Returns
    {pool_key: [(tick, liquidityNet), ...]}.
    """
    if not pools_with_ranges:
        return {}

    per_pool_words: list[list[int]] = []
    word_calls = []
    for pool, tick_lo, tick_hi in pools_with_ranges:
        pool_id_bytes = bytes.fromhex(pool.pool_key[2:])
        words = list(tick_bitmap.word_range_for_ticks(tick_lo, tick_hi, pool.tick_spacing))
        per_pool_words.append(words)
        word_calls.extend(_extsload_call(_tick_bitmap_slot(pool_id_bytes, w)) for w in words)

    word_raw = batch_eth_call(w3, word_calls)

    per_pool_candidates: list[list[int]] = []
    idx = 0
    for (pool, tick_lo, tick_hi), words in zip(pools_with_ranges, per_pool_words):
        candidates = []
        for word_pos in words:
            bitmap = _word_to_int(word_raw[idx])
            idx += 1
            for t in tick_bitmap.initialized_ticks_in_word(word_pos, bitmap, pool.tick_spacing):
                if tick_lo <= t <= tick_hi:
                    candidates.append(t)
        per_pool_candidates.append(candidates)

    tick_calls = []
    for (pool, _, _), candidates in zip(pools_with_ranges, per_pool_candidates):
        pool_id_bytes = bytes.fromhex(pool.pool_key[2:])
        tick_calls.extend(_extsload_call(_tick_info_slot(pool_id_bytes, t)) for t in candidates)

    tick_raw = batch_eth_call(w3, tick_calls)

    result: dict[str, list[tuple[int, int]]] = {}
    idx = 0
    for (pool, _, _), candidates in zip(pools_with_ranges, per_pool_candidates):
        entries = []
        for t in candidates:
            raw = tick_raw[idx]
            idx += 1
            _, liquidity_net = _parse_tick_info_word(_word_to_int(raw))
            entries.append((t, liquidity_net))
        result[pool.pool_key] = entries
    return result

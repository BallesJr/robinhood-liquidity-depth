"""Combine V3 + V4 pools into a shared USD-price liquidity depth chart and
the buyside/sellside imbalance metric.

Pipeline for a single token:
 1. Discover pools (pools_v3 / pools_v4) and read each pool's current state.
 2. Get a rough current USD price: the median of each pool's price,
    weighted by its active depth (see _active_depth_usd).
 3. Turn +/-window% around that rough price into a per-pool tick range
    (each pool has its own raw price domain, and target-vs-quote can be on
    either side of the pair, which flips whether price increases or
    decreases with tick - see _make_price_converters).
 4. Fetch initialized ticks in that range and reconstruct active liquidity
    per segment (liquidity_math.reconstruct_liquidity_segments).
 5. Value each segment in USD using the *current* USD price of both tokens
    (not a per-segment hypothetical price - see docs/METHODOLOGY.md).
 6. Bucket every segment into one of `num_bins` shared USD-price bins by
    its midpoint price, stacked by pool.
 7. Refine the reported "current price" as the active-depth-weighted
    average of the freshly refreshed pool prices (falls back to the rough
    price if no pool has active liquidity).
 8. Buyside $ / sellside $ = liquidity segments below / above the refined
    current price (computed per segment, so independent of num_bins).
"""
from __future__ import annotations

import bisect
import logging
import statistics
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

from web3 import Web3

from . import config, liquidity_math, pools_v3, pools_v4, price_check
from .models import AnalysisResult, BinRow, PoolInfo, PoolResult
from .tokens import get_quote_token_usd_price, get_token_meta

logger = logging.getLogger("liquidity_depth")


def _make_price_converters(pool: PoolInfo, target_address: str, quote_usd_price: float):
    """Return (tick_to_target_usd, target_usd_to_tick) for this pool.

    Whether the target token's USD price moves the same way as the pool's
    raw (token1-per-token0) price, or inversely, depends on which side of
    the pair the target token is on.
    """
    target_is_token0 = pool.token0.address.lower() == target_address.lower()
    dec0, dec1 = pool.token0.decimals, pool.token1.decimals

    def tick_to_target_usd(tick: float) -> float:
        human = liquidity_math.human_price_at_tick(tick, dec0, dec1)
        return human * quote_usd_price if target_is_token0 else quote_usd_price / human

    def target_usd_to_tick(target_price: float) -> float:
        human_needed = target_price / quote_usd_price if target_is_token0 else quote_usd_price / target_price
        return liquidity_math.tick_at_human_price(human_needed, dec0, dec1)

    return target_is_token0, tick_to_target_usd, target_usd_to_tick


def _current_target_price(pool: PoolInfo, target_is_token0: bool, quote_usd_price: float) -> float:
    dec0, dec1 = pool.token0.decimals, pool.token1.decimals
    human = liquidity_math.human_price_from_sqrt_price_x96(pool.sqrt_price_x96, dec0, dec1)
    return human * quote_usd_price if target_is_token0 else quote_usd_price / human


def _range_usd(pool: PoolInfo, tick_lo: float, tick_hi: float, liquidity: int, price0_usd: float, price1_usd: float) -> float:
    """USD value of `liquidity` spread over [tick_lo, tick_hi] in `pool`,
    at the pool's current price, valuing both tokens at today's USD price."""
    sqrt_now = liquidity_math.sqrt_price_x96_to_sqrt_raw_price(pool.sqrt_price_x96)
    amount0_raw, amount1_raw = liquidity_math.segment_token_amounts(tick_lo, tick_hi, liquidity, pool.tick, sqrt_now)
    return (amount0_raw / 10**pool.token0.decimals) * price0_usd + (amount1_raw / 10**pool.token1.decimals) * price1_usd


def _active_depth_usd(pool: PoolInfo, target_is_token0: bool, quote_usd_price: float) -> float:
    """How much a pool's current price should count when combining pools:
    the USD value of the quote side of its *virtual* reserves at the
    current price (L * sqrtP for token1, L / sqrtP for token0). This is
    proportional to how much it costs to move that pool's price, so it is
    comparable across pools with different pairs and decimals.

    A pool with zero active liquidity gets weight 0: its price is just the
    last trade before its liquidity left, possibly long ago. Abandoned pools
    like this used to drag the plain median far from the real price - for
    MOO, three dead V3 pools at $0.018-0.027 put the whole window
    ($0.017-0.028) above the only live pool's $0.0104, so the current price
    was off the chart and buyside read $0.
    """
    if pool.liquidity <= 0 or pool.sqrt_price_x96 <= 0:
        return 0.0
    sqrt_raw = liquidity_math.sqrt_price_x96_to_sqrt_raw_price(pool.sqrt_price_x96)
    if target_is_token0:
        quote_amount = pool.liquidity * sqrt_raw / 10**pool.token1.decimals
    else:
        quote_amount = pool.liquidity / sqrt_raw / 10**pool.token0.decimals
    return quote_amount * quote_usd_price


def _auto_num_bins(prepared: list[tuple], rough_price: float, price_low: float, price_high: float) -> int:
    """Pick the chart's bar count from how fine the token's liquidity
    actually is: one bar per tick-spacing step of the pools that matter.

    A pool's liquidity can only change at its initialized ticks, which sit
    a multiple of tick_spacing apart - each step moves the price by
    1.0001**tick_spacing. Bars much narrower than that just repeat the same
    flat step; much wider ones hide real detail. The step used is the
    finest one shared by at least a quarter of the active depth (weighted
    25th percentile, so dust pools don't decide it): a plain median hid
    $AI's detail, where 34% of the depth sits in pools with 0.08-0.6% steps
    but the median pool steps by 2%. The count is clamped to
    AUTO_BINS_MIN..AUTO_BINS_MAX to stay readable. Display-only - see
    segment_records.
    """
    steps = [
        (1.0001 ** pool.tick_spacing - 1, _active_depth_usd(pool, target_is_token0, get_quote_token_usd_price(quote_meta.address)))
        for pool, quote_meta, target_is_token0, _t2u, _u2t, current_price in prepared
        if current_price is not None and pool.tick_spacing > 0
    ]
    step = _weighted_quantile(steps, 0.25)
    if not step:
        return config.AUTO_BINS_MAX
    bars = round((price_high - price_low) / (rough_price * step))
    return min(max(bars, config.AUTO_BINS_MIN), config.AUTO_BINS_MAX)


def _weighted_quantile(values_and_weights: list[tuple[float, float]], q: float) -> float | None:
    """The `q` quantile (0-1) of `values` where each counts `weight` times;
    None if every weight is zero."""
    total = sum(w for _, w in values_and_weights)
    if total <= 0:
        return None
    cumulative = 0.0
    for value, weight in sorted(values_and_weights):
        cumulative += weight
        if cumulative >= total * q:
            return value
    return None  # pragma: no cover - unreachable with total > 0


def _weighted_median(values_and_weights: list[tuple[float, float]]) -> float | None:
    return _weighted_quantile(values_and_weights, 0.5)


# Phases reported through analyze_token's `progress` callback, in order.
ANALYSIS_STEPS = (
    "Reading token metadata",
    "Discovering V3 pools",
    "Discovering V4 pools",
    "Reading pool state and ticks",
    "Computing liquidity depth",
    "Cross-checking price",
)


def _run_v3_v4_in_parallel(v3_task: Callable[[], object] | None, v4_task: Callable[[], object] | None) -> list:
    """Run the V3 and V4 halves of a phase at the same time. With
    Multicall3 each half is usually a single eth_call per step, so running
    them side by side stays well within the RPC's calls/s limit while
    halving the round trips - see docs/METHODOLOGY.md "Performance notes".
    Exceptions propagate to the caller, same as running them in sequence.
    """
    tasks = [t for t in (v3_task, v4_task) if t is not None]
    if len(tasks) <= 1:
        return [t() for t in tasks]
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(t) for t in tasks]
        return [f.result() for f in futures]


def _refresh_states_batch(w3, pools: list[PoolInfo], target_address: str) -> None:
    """Dispatch a batch refresh across both versions at once - see
    pools_v3.refresh_states_batch's docstring for why this matters:
    refreshing pool-by-pool meant 1-2 HTTP round trips *per pool*; this
    brings the whole set down to one round trip per version, run in parallel.
    """
    v3 = [p for p in pools if p.version == "v3"]
    v4 = [p for p in pools if p.version == "v4"]
    _run_v3_v4_in_parallel(
        (lambda: pools_v3.refresh_states_batch(w3, v3, target_address)) if v3 else None,
        (lambda: pools_v4.refresh_states_batch(w3, v4)) if v4 else None,
    )


def _fetch_ticks_batch(w3, pools_with_ranges: list[tuple[PoolInfo, int, int]]) -> dict[str, list[tuple[int, int]]]:
    """Dispatch a batch tick fetch across both versions at once - same
    round-trip reasoning as _refresh_states_batch.
    """
    v3 = [pr for pr in pools_with_ranges if pr[0].version == "v3"]
    v4 = [pr for pr in pools_with_ranges if pr[0].version == "v4"]
    result: dict[str, list[tuple[int, int]]] = {}
    for part in _run_v3_v4_in_parallel(
        (lambda: pools_v3.fetch_initialized_ticks_batch(w3, v3)) if v3 else None,
        (lambda: pools_v4.fetch_initialized_ticks_batch(w3, v4)) if v4 else None,
    ):
        result.update(part)
    return result


def analyze_token(
    w3,
    token_address: str,
    window_pct: float | None = None,
    num_bins: int | None = None,
    use_cache: bool = True,
    progress: Callable[[int, str], None] | None = None,
) -> AnalysisResult:
    """Run the full pipeline for one token (see the module docstring).

    `progress`, if given, is called as progress(step, label) at the start of
    each of the ANALYSIS_STEPS phases, e.g. to drive a UI progress bar.
    """
    window_pct = config.DEFAULT_WINDOW_PCT if window_pct is None else window_pct
    num_bins = config.DEFAULT_NUM_BINS if num_bins is None else num_bins  # still None = auto

    def report(label: str) -> None:
        if progress is not None:
            progress(ANALYSIS_STEPS.index(label) + 1, label)

    report("Reading token metadata")

    token_address = Web3.to_checksum_address(token_address)
    token_meta = get_token_meta(w3, token_address)

    report("Discovering V3 pools")
    logger.info("Discovering V3 pools for %s (%s)...", token_meta.symbol, token_address)
    v3_pools = pools_v3.discover_v3_pools(w3, token_address)
    report("Discovering V4 pools")
    logger.info("Discovering V4 pools for %s (%s)...", token_meta.symbol, token_address)
    v4_pools = pools_v4.discover_v4_pools(w3, token_address, use_cache=use_cache)
    all_pools = [*v3_pools, *v4_pools]

    if not all_pools:
        raise ValueError(f"No Uniswap V3/V4 pools found for token {token_address} against the configured quote tokens")

    # Step 1: rough current price per priceable pool.
    prepared = []  # (pool, quote_token_meta, target_is_token0, tick_to_usd, usd_to_tick, current_price)
    for pool in all_pools:
        target_is_token0 = pool.token0.address.lower() == token_address.lower()
        quote_meta = pool.token1 if target_is_token0 else pool.token0
        quote_price = get_quote_token_usd_price(quote_meta.address)
        if quote_price is None:
            logger.warning("No USD price available for quote token %s (%s); skipping pool %s", quote_meta.symbol, quote_meta.address, pool.pool_key)
            prepared.append((pool, quote_meta, target_is_token0, None, None, None))
            continue
        _, tick_to_usd, usd_to_tick = _make_price_converters(pool, token_address, quote_price)
        current_price = _current_target_price(pool, target_is_token0, quote_price)
        prepared.append((pool, quote_meta, target_is_token0, tick_to_usd, usd_to_tick, current_price))

    priceable_prices = [p[5] for p in prepared if p[5] is not None]
    if not priceable_prices:
        raise ValueError(
            "Found pools for this token, but none of their quote tokens have a resolvable USD price "
            "(configure a price source in tokens.py or add liquid stablecoin/major pairs)."
        )
    # Weighted by active depth so dead/dust pools can't move the window -
    # see _active_depth_usd. Plain median only if no pool has any depth.
    rough_price = _weighted_median([
        (current_price, _active_depth_usd(pool, target_is_token0, get_quote_token_usd_price(quote_meta.address)))
        for pool, quote_meta, target_is_token0, _t2u, _u2t, current_price in prepared
        if current_price is not None
    ]) or statistics.median(priceable_prices)
    # A window of 100% or more would reach $0 or below on the downside; a
    # price can't go negative, so the window then simply starts at $0.
    price_low = max(rough_price * (1 - window_pct), 0.0)
    price_high = rough_price * (1 + window_pct)

    if num_bins is None:
        num_bins = _auto_num_bins(prepared, rough_price, price_low, price_high)

    # Step 2: per-pool tick range + tick fetch + segment reconstruction.
    bins = [
        BinRow(
            price_low=price_low + i * (price_high - price_low) / num_bins,
            price_high=price_low + (i + 1) * (price_high - price_low) / num_bins,
            price_mid=price_low + (i + 0.5) * (price_high - price_low) / num_bins,
            usd_by_pool={},
        )
        for i in range(num_bins)
    ]
    bin_width = (price_high - price_low) / num_bins

    def bin_index(price: float) -> int:
        idx = int((price - price_low) / bin_width)
        return min(max(idx, 0), num_bins - 1)

    pool_results: list[PoolResult] = []
    weighted_price_sum = 0.0
    weight_total = 0.0
    # One (mid_price, usd_value) entry per liquidity segment, at the pool's
    # own tick granularity - NOT grouped into `bins`. Buyside/sellside must
    # be computed from this, never from `bins`: `bins` exist only to draw
    # the chart, and their width is set by `num_bins` (a display parameter).
    # Summing whole *bins* by their midpoint would let the one bin that
    # straddles the current price get counted entirely on one side even
    # though part of its liquidity sits on the other - and coarser bins
    # (fewer of them) make that misclassified slice bigger, so the
    # imbalance figure would drift depending on how the chart happens to be
    # drawn. Segments don't have that problem: their boundaries come from
    # each pool's actual initialized ticks, so a segment never straddles an
    # arbitrary chart boundary.
    segment_records: list[tuple[float, float]] = []

    # Step 2a: which pools are priceable, and their tick range - pure
    # Python, no RPC. usd_to_tick only depends on the (already-fetched)
    # quote price and the pool's decimals, not its live state, so this
    # doesn't need fresh on-chain data yet.
    priceable: list[tuple] = []  # (pool, quote_meta, target_is_token0, tick_to_usd, usd_to_tick, tick_lo, tick_hi)
    for pool, quote_meta, target_is_token0, tick_to_usd, usd_to_tick, _current_price in prepared:
        if tick_to_usd is None:
            pool_results.append(
                PoolResult(
                    pool=pool,
                    quote_token=quote_meta,
                    target_is_token0=target_is_token0,
                    current_target_price_usd=None,
                    liquidity_in_window_usd=0.0,
                    priceable=False,
                    skip_reason=f"no USD price for quote token {quote_meta.symbol}",
                )
            )
            continue

        # A $0 target price is the extreme end of the pool's tick range:
        # MIN_TICK when the target is token0 (price = token1 per token0),
        # MAX_TICK when it's token1 (price is inverted).
        if price_low > 0:
            tick_a = usd_to_tick(price_low)
        else:
            tick_a = liquidity_math.MIN_TICK if target_is_token0 else liquidity_math.MAX_TICK
        tick_b = usd_to_tick(price_high)
        tick_lo = liquidity_math.clamp_tick(min(tick_a, tick_b))
        tick_hi = liquidity_math.clamp_tick(max(tick_a, tick_b))
        if tick_lo == tick_hi:
            tick_hi = min(tick_lo + pool.tick_spacing, liquidity_math.MAX_TICK)
        priceable.append((pool, quote_meta, target_is_token0, tick_to_usd, usd_to_tick, tick_lo, tick_hi))

    # Step 2b: refresh state for every pool, then fetch ticks for every
    # pool, each as one batched round trip per version rather than one
    # round trip *per pool* - see _refresh_states_batch/_fetch_ticks_batch.
    # Still refreshes immediately before fetching (the whole batch
    # completes in a couple of seconds, nowhere near the "minutes" that
    # caused the original temporal-consistency bug - see
    # pools_v3/pools_v4 refresh_state docstrings), so the same
    # discovery-vs-tick-fetch consistency guarantee holds, just applied to
    # the whole set at once instead of pool-by-pool.
    report("Reading pool state and ticks")
    failed_keys: set[str] = set()
    all_priceable_pools = [p[0] for p in priceable]
    try:
        _refresh_states_batch(w3, all_priceable_pools, token_address)
    except Exception as exc:  # noqa: BLE001 - one bad batch shouldn't crash the whole run
        logger.warning("Batch refresh failed for %d pools: %s", len(all_priceable_pools), exc)
        failed_keys.update(p.pool_key for p in all_priceable_pools)

    ranges = [(pool, tick_lo, tick_hi) for pool, *_rest, tick_lo, tick_hi in priceable if pool.pool_key not in failed_keys]
    ticks_by_key: dict[str, list[tuple[int, int]]] = {}
    try:
        ticks_by_key = _fetch_ticks_batch(w3, ranges)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Batch tick fetch failed for %d pools: %s", len(ranges), exc)
        failed_keys.update(pool.pool_key for pool, *_ in ranges)

    report("Computing liquidity depth")
    # Step 2c: per-pool segment reconstruction + USD valuation + binning,
    # using the batch-refreshed state / batch-fetched ticks from above.
    for pool, quote_meta, target_is_token0, tick_to_usd, usd_to_tick, tick_lo, tick_hi in priceable:
        if pool.pool_key in failed_keys or pool.sqrt_price_x96 == 0:
            pool_results.append(
                PoolResult(
                    pool=pool,
                    quote_token=quote_meta,
                    target_is_token0=target_is_token0,
                    current_target_price_usd=None,
                    liquidity_in_window_usd=0.0,
                    priceable=False,
                    skip_reason="batch refresh/tick fetch failed",
                )
            )
            continue

        quote_price = get_quote_token_usd_price(quote_meta.address)
        current_price = _current_target_price(pool, target_is_token0, quote_price)
        initialized_ticks = ticks_by_key.get(pool.pool_key, [])

        segments = liquidity_math.reconstruct_liquidity_segments(
            current_tick=pool.tick,
            current_liquidity=pool.liquidity,
            initialized_ticks=initialized_ticks,
            tick_lo_bound=tick_lo,
            tick_hi_bound=tick_hi,
        )

        price0_usd = current_price if target_is_token0 else quote_price
        price1_usd = quote_price if target_is_token0 else current_price

        # The chart's bin edges in this pool's tick space, ascending (tick
        # and USD price run in opposite directions when the target is
        # token1, hence the sort).
        edge_ticks = sorted(usd_to_tick(price_low + i * bin_width) for i in range(1, num_bins))

        pool_total_usd = 0.0
        for seg_lo, seg_hi, seg_liquidity in segments:
            if seg_liquidity <= 0 or seg_hi <= seg_lo:
                continue
            usd_value = _range_usd(pool, seg_lo, seg_hi, seg_liquidity, price0_usd, price1_usd)
            if usd_value <= 0:
                continue
            pool_total_usd += usd_value
            segment_records.append((tick_to_usd((seg_lo + seg_hi) / 2), usd_value))

            # Chart only: spread the segment over every bin it covers. A
            # wide segment (full-range position, huge tick spacing) used to
            # land entirely in the bin holding its midpoint, drawing one
            # spike where the liquidity is actually spread thin.
            cuts = edge_ticks[bisect.bisect_right(edge_ticks, seg_lo) : bisect.bisect_left(edge_ticks, seg_hi)]
            for lo, hi in zip([seg_lo, *cuts], [*cuts, seg_hi], strict=True):
                part = _range_usd(pool, lo, hi, seg_liquidity, price0_usd, price1_usd)
                if part > 0:
                    idx = bin_index(tick_to_usd((lo + hi) / 2))
                    bins[idx].usd_by_pool[pool.pool_key] = bins[idx].usd_by_pool.get(pool.pool_key, 0.0) + part

        pool_results.append(
            PoolResult(
                pool=pool,
                quote_token=quote_meta,
                target_is_token0=target_is_token0,
                current_target_price_usd=current_price,
                liquidity_in_window_usd=pool_total_usd,
                priceable=True,
            )
        )
        # Weighted by active depth (from the just-refreshed state), not by
        # liquidity in window: a dead pool can still hold out-of-range
        # positions inside the window, and its stale price must not count.
        depth = _active_depth_usd(pool, target_is_token0, quote_price)
        weighted_price_sum += current_price * depth
        weight_total += depth

    refined_price = (weighted_price_sum / weight_total) if weight_total > 0 else rough_price

    # Bin-independent by construction - see segment_records' comment above.
    buyside_usd = sum(usd for mid_price, usd in segment_records if mid_price < refined_price)
    sellside_usd = sum(usd for mid_price, usd in segment_records if mid_price >= refined_price)

    report("Cross-checking price")
    try:
        # Best-effort sanity check against an independent price source; a
        # network hiccup here must never fail the whole analysis.
        price_check_result = price_check.check_price(token_address, refined_price)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Price cross-check failed: %s", exc)
        price_check_result = None

    return AnalysisResult(
        token=token_meta,
        current_price_usd=refined_price,
        price_low=price_low,
        price_high=price_high,
        bins=bins,
        pool_results=pool_results,
        buyside_usd=buyside_usd,
        sellside_usd=sellside_usd,
        imbalance_usd=buyside_usd - sellside_usd,
        price_check=price_check_result,
    )

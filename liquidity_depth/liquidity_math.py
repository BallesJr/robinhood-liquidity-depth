"""Uniswap V3/V4 concentrated-liquidity math.

Formulas follow the Uniswap V3 whitepaper and the official contracts
(TickMath.sol / LiquidityAmounts.sol) - the same math both @uniswap/v3-sdk
and @uniswap/v4-sdk implement, and that V4 reuses unchanged from V3. There
is no maintained, pip-installable Python port of those SDKs, so the
formulas are reimplemented here directly from the whitepaper rather than
depending on a shaky/unofficial package; they are the well-known five
identities every third-party Uniswap analytics tool (including Uniswap's
own subgraph mappings) reimplements the same way.

Two important unit conventions used throughout this module:

* "raw" price/amounts are in atomic token units with NO decimals
  adjustment - this is the domain `liquidity` (L) itself is defined in,
  so all L-based formulas must stay in this domain.
* "human" price applies the (decimals0 - decimals1) adjustment, giving a
  real-world price ratio - used only for display and for USD bucketing,
  never mixed into the L-based formulas.
"""
from __future__ import annotations

import math

TICK_BASE = 1.0001
MIN_TICK = -887272
MAX_TICK = 887272


def raw_price_at_tick(tick: float) -> float:
    """token1 per token0, in atomic units, with no decimals adjustment."""
    return TICK_BASE**tick


def tick_at_raw_price(price: float) -> float:
    return math.log(price, TICK_BASE)


def sqrt_raw_price_at_tick(tick: float) -> float:
    return TICK_BASE ** (tick / 2)


def sqrt_price_x96_to_sqrt_raw_price(sqrt_price_x96: int) -> float:
    return sqrt_price_x96 / (2**96)


def human_price_at_tick(tick: float, decimals0: int, decimals1: int) -> float:
    """Real-world price of 1 token0 in units of token1."""
    return raw_price_at_tick(tick) * (10 ** (decimals0 - decimals1))


def human_price_from_sqrt_price_x96(sqrt_price_x96: int, decimals0: int, decimals1: int) -> float:
    """Same as human_price_at_tick, but derived from the exact sqrtPriceX96
    rather than the (floored) tick index - used for the current spot price.
    """
    sqrt_raw = sqrt_price_x96_to_sqrt_raw_price(sqrt_price_x96)
    return (sqrt_raw**2) * (10 ** (decimals0 - decimals1))


def tick_at_human_price(price_human: float, decimals0: int, decimals1: int) -> float:
    raw = price_human / (10 ** (decimals0 - decimals1))
    return tick_at_raw_price(raw)


def clamp_tick(tick: float) -> int:
    return int(min(max(tick, MIN_TICK), MAX_TICK))


def segment_token_amounts(
    tick_lower: int,
    tick_upper: int,
    liquidity: float,
    current_tick: int,
    current_sqrt_raw_price: float | None = None,
) -> tuple[float, float]:
    """Atomic-unit (amount0, amount1) reserves represented by `liquidity`
    active uniformly across [tick_lower, tick_upper], given where the
    pool's current price sits relative to that range.

    Mirrors LiquidityAmounts.getAmountsForLiquidity from Uniswap V3 core.
    """
    sqrt_a = sqrt_raw_price_at_tick(tick_lower)
    sqrt_b = sqrt_raw_price_at_tick(tick_upper)
    if sqrt_a > sqrt_b:
        sqrt_a, sqrt_b = sqrt_b, sqrt_a

    # Compare by exact price, not by tick, like LiquidityAmounts does: the
    # live price sits *inside* its tick, so a range whose lower bound is
    # the current tick still has the price within it (and holds a sliver
    # of token1), even though current_tick == tick_lower.
    sqrt_p = current_sqrt_raw_price if current_sqrt_raw_price is not None else sqrt_raw_price_at_tick(current_tick)
    if sqrt_p <= sqrt_a:
        # Current price below the whole range: range holds only token0.
        amount0 = liquidity * (1 / sqrt_a - 1 / sqrt_b)
        return amount0, 0.0
    if sqrt_p >= sqrt_b:
        # Current price above the whole range: range holds only token1.
        amount1 = liquidity * (sqrt_b - sqrt_a)
        return 0.0, amount1

    # Current price inside the range: split between both tokens.
    amount0 = liquidity * (1 / sqrt_p - 1 / sqrt_b)
    amount1 = liquidity * (sqrt_p - sqrt_a)
    return amount0, amount1


def reconstruct_liquidity_segments(
    current_tick: int,
    current_liquidity: int,
    initialized_ticks: list[tuple[int, int]],
    tick_lo_bound: int,
    tick_hi_bound: int,
) -> list[tuple[int, int, float]]:
    """Turn a pool's current active liquidity plus a set of initialized
    ticks (tick, liquidityNet) into a list of (seg_lower, seg_upper,
    active_liquidity) segments spanning [tick_lo_bound, tick_hi_bound].

    Standard reconstruction: liquidity increases by liquidityNet when price
    crosses a tick moving up, and decreases by the same amount moving down.
    Since `current_liquidity` is already the correct value for whichever
    segment contains the live price, segments are built outward from there.
    """
    points = {t for t, _ in initialized_ticks if tick_lo_bound < t < tick_hi_bound}
    # Also split at the current tick (it carries no liquidityNet, so the
    # liquidity is the same on both sides). Otherwise the segment holding
    # the live price straddles it and gets counted entirely as buyside or
    # sellside by its midpoint - for a wide position (full-range, or a V4
    # pool with a tick spacing in the thousands) that's the pool's whole
    # in-window liquidity on one side. Splitting here keeps the token
    # amounts exact: the lower half is wholly below the price and the upper
    # half starts at the tick the price is in.
    if tick_lo_bound < current_tick < tick_hi_bound:
        points.add(current_tick)
    points = sorted(points)
    liq_net_by_tick = dict(initialized_ticks)

    boundaries = [tick_lo_bound, *points, tick_hi_bound]
    n_segments = len(boundaries) - 1
    liquidities: list[float | None] = [None] * n_segments

    # Uniswap treats a position as active when tickLower <= tick < tickUpper,
    # so a price sitting exactly on an initialized tick belongs to the
    # segment *starting* there. Out-of-window prices clamp to the edge segment.
    cur_idx = n_segments - 1
    for i in range(n_segments):
        if current_tick < boundaries[i + 1]:
            cur_idx = i
            break
    liquidities[cur_idx] = current_liquidity

    liq = current_liquidity
    for i in range(cur_idx + 1, n_segments):
        liq = liq + liq_net_by_tick.get(boundaries[i], 0)
        liquidities[i] = liq

    liq = current_liquidity
    for i in range(cur_idx - 1, -1, -1):
        liq = liq - liq_net_by_tick.get(boundaries[i + 1], 0)
        liquidities[i] = liq

    return [(boundaries[i], boundaries[i + 1], liquidities[i]) for i in range(n_segments)]

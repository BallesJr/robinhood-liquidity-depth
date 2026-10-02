import pytest

from liquidity_depth import liquidity_math as lm

# Positions used below: [-100, 100) with L=10 and [0, 200) with L=5.
TICKS = [(-100, 10), (0, 5), (100, -10), (200, -5)]
EXPECTED_SEGMENTS = [(-150, -100, 0), (-100, 0, 10), (0, 100, 15), (100, 200, 5), (200, 250, 0)]


def test_tick_price_round_trip_with_decimals():
    # e.g. an 18-decimal token priced in a 6-decimal stablecoin
    tick = lm.tick_at_human_price(0.15, 18, 6)
    assert lm.human_price_at_tick(tick, 18, 6) == pytest.approx(0.15)


def test_sqrt_price_x96_matches_tick_price():
    tick = 12345
    sqrt_price_x96 = int(lm.sqrt_raw_price_at_tick(tick) * 2**96)
    assert lm.human_price_from_sqrt_price_x96(sqrt_price_x96, 18, 18) == pytest.approx(
        lm.human_price_at_tick(tick, 18, 18)
    )


def test_clamp_tick():
    assert lm.clamp_tick(-1e9) == lm.MIN_TICK
    assert lm.clamp_tick(1e9) == lm.MAX_TICK
    assert lm.clamp_tick(42.7) == 42


def test_segment_amounts_by_price_position():
    liquidity = 1e18
    below = lm.segment_token_amounts(100, 200, liquidity, current_tick=50)
    above = lm.segment_token_amounts(100, 200, liquidity, current_tick=250)
    inside = lm.segment_token_amounts(100, 200, liquidity, current_tick=150)
    assert below[0] > 0 and below[1] == 0  # range above the price: all token0
    assert above[0] == 0 and above[1] > 0  # range below the price: all token1
    assert inside[0] > 0 and inside[1] > 0
    # Whitepaper: amount1 = L * (sqrt(pb) - sqrt(pa)) for a range fully in token1
    expected = liquidity * (lm.sqrt_raw_price_at_tick(200) - lm.sqrt_raw_price_at_tick(100))
    assert above[1] == pytest.approx(expected)


def test_reconstruct_segments_walks_liquidity_net_both_ways():
    segs = lm.reconstruct_liquidity_segments(
        current_tick=50, current_liquidity=15, initialized_ticks=TICKS, tick_lo_bound=-150, tick_hi_bound=250
    )
    # Same as EXPECTED_SEGMENTS, but the active segment is split at the
    # current tick so it doesn't straddle the price.
    assert segs == [(-150, -100, 0), (-100, 0, 10), (0, 50, 15), (50, 100, 15), (100, 200, 5), (200, 250, 0)]


def test_reconstruct_segments_price_exactly_on_initialized_tick():
    # At tick 0 the [0, 200) position is already active (tickLower <= tick),
    # so the pool reports L=15, which belongs to the segment starting at 0.
    segs = lm.reconstruct_liquidity_segments(
        current_tick=0, current_liquidity=15, initialized_ticks=TICKS, tick_lo_bound=-150, tick_hi_bound=250
    )
    assert segs == EXPECTED_SEGMENTS


def test_reconstruct_segments_price_outside_window():
    segs = lm.reconstruct_liquidity_segments(
        current_tick=400, current_liquidity=0, initialized_ticks=TICKS, tick_lo_bound=-150, tick_hi_bound=250
    )
    assert segs == EXPECTED_SEGMENTS


def test_reconstruct_segments_without_ticks_splits_only_at_current_tick():
    # e.g. a single full-range position: without the split, the whole
    # window would be one segment counted entirely on one side of the price.
    assert lm.reconstruct_liquidity_segments(7, 99, [], -10, 10) == [(-10, 7, 99), (7, 10, 99)]


def test_split_at_current_tick_keeps_token_amounts_exact():
    current_tick = 7
    sqrt_now = lm.sqrt_raw_price_at_tick(7.4)  # live price sits inside tick 7
    whole = lm.segment_token_amounts(-10, 10, 99, current_tick, sqrt_now)
    halves = [lm.segment_token_amounts(lo, hi, liq, current_tick, sqrt_now) for lo, hi, liq in
              lm.reconstruct_liquidity_segments(current_tick, 99, [], -10, 10)]
    assert sum(h[0] for h in halves) == pytest.approx(whole[0])
    assert sum(h[1] for h in halves) == pytest.approx(whole[1])

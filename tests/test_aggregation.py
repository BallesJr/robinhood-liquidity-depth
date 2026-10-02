import pytest

from liquidity_depth import aggregation, config, liquidity_math
from liquidity_depth.models import PoolInfo, TokenMeta

TARGET = TokenMeta(address="0x" + "aa" * 20, symbol="TGT", name="Target", decimals=18)
USD = TokenMeta(address="0x" + "bb" * 20, symbol="USD", name="Dollar", decimals=6)


def _pool(target_usd_price: float, liquidity: int, target_is_token0: bool = True) -> PoolInfo:
    token0, token1 = (TARGET, USD) if target_is_token0 else (USD, TARGET)
    human = target_usd_price if target_is_token0 else 1 / target_usd_price
    tick = liquidity_math.tick_at_human_price(human, token0.decimals, token1.decimals)
    sqrt_price_x96 = int(liquidity_math.sqrt_raw_price_at_tick(tick) * 2**96)
    return PoolInfo(
        version="v3", pool_key="0x" + "cc" * 20, fee=3000, tick_spacing=60, hooks=None,
        token0=token0, token1=token1, sqrt_price_x96=sqrt_price_x96, tick=int(tick),
        liquidity=liquidity, tvl_usd=None,
    )


def test_weighted_median_ignores_zero_weight_values():
    # Three dead pools at stale prices, one live pool: the live one wins.
    assert aggregation._weighted_median([(0.027, 0.0), (0.018, 0.0), (0.0104, 5.0), (0.0277, 0.0)]) == 0.0104


def test_weighted_median_follows_the_heaviest_side():
    assert aggregation._weighted_median([(1.0, 1.0), (2.0, 1.0), (3.0, 10.0)]) == 3.0
    assert aggregation._weighted_median([(1.0, 0.0)]) is None


def test_dead_pool_has_no_active_depth():
    assert aggregation._active_depth_usd(_pool(0.02, liquidity=0), True, 1.0) == 0.0


def test_active_depth_is_symmetric_in_token_order():
    # The same pool expressed with the target as token0 or token1 must get
    # the same weight, since it's the same amount of tradeable liquidity.
    a = aggregation._active_depth_usd(_pool(0.01, liquidity=10**21, target_is_token0=True), True, 1.0)
    b = aggregation._active_depth_usd(_pool(0.01, liquidity=10**21, target_is_token0=False), False, 1.0)
    assert a > 0
    assert abs(a - b) / a < 1e-3


@pytest.fixture
def one_wide_pool(monkeypatch):
    """A single USDG-quoted pool (fixed $1 peg, so no network) with no
    initialized ticks - i.e. one position covering the whole window.
    Returns the list the tick-fetch ranges get recorded into."""
    usdg = TokenMeta(address=config.USDG, symbol="USDG", name="Global Dollar", decimals=6)
    pool = _pool(0.02, liquidity=10**21)
    pool.token1 = usdg
    ranges = []
    monkeypatch.setattr(aggregation, "get_token_meta", lambda w3, a: TARGET)
    monkeypatch.setattr(aggregation.pools_v3, "discover_v3_pools", lambda w3, a: [pool])
    monkeypatch.setattr(aggregation.pools_v4, "discover_v4_pools", lambda w3, a, use_cache: [])
    monkeypatch.setattr(aggregation, "_refresh_states_batch", lambda w3, pools, a: None)
    monkeypatch.setattr(aggregation, "_fetch_ticks_batch", lambda w3, prs: ranges.extend(prs) or {})
    monkeypatch.setattr(aggregation.price_check, "check_price", lambda a, p: None)
    return ranges


@pytest.mark.parametrize("num_bins", [10, 37, 200])
def test_wide_position_is_spread_over_every_bin_it_covers(one_wide_pool, num_bins):
    result = aggregation.analyze_token(None, TARGET.address, window_pct=0.5, num_bins=num_bins)

    per_bin = [sum(b.usd_by_pool.values()) for b in result.bins]
    assert all(v > 0 for v in per_bin)  # no single spike at the midpoint
    assert sum(per_bin) == pytest.approx(result.buyside_usd + result.sellside_usd)


def test_buy_and_sell_dont_depend_on_bin_count(one_wide_pool):
    a = aggregation.analyze_token(None, TARGET.address, window_pct=0.5, num_bins=10)
    b = aggregation.analyze_token(None, TARGET.address, window_pct=0.5, num_bins=200)
    assert (a.buyside_usd, a.sellside_usd) == pytest.approx((b.buyside_usd, b.sellside_usd))


@pytest.mark.parametrize("window_pct", [0.5, 1.0, 2.0])
def test_window_of_100_percent_or_more_starts_at_zero(one_wide_pool, window_pct):
    ranges = one_wide_pool
    result = aggregation.analyze_token(None, TARGET.address, window_pct=window_pct, num_bins=10)

    assert result.price_low == pytest.approx(max(0.02 * (1 - window_pct), 0.0))
    (_, tick_lo, _), = ranges
    if window_pct >= 1:
        assert tick_lo == liquidity_math.MIN_TICK  # target is token0: $0 is the lowest tick
    assert result.buyside_usd > 0 and result.sellside_usd > 0


def _prepared(pool):
    usdg = TokenMeta(address=config.USDG, symbol="USDG", name="Global Dollar", decimals=6)
    pool.token1 = usdg
    return [(pool, usdg, True, None, None, 0.02)]


@pytest.mark.parametrize(
    "tick_spacing, window_pct, expected",
    [
        (200, 0.25, 40),  # ~2% steps: 25 bars, clamped up to the minimum
        (60, 0.25, 83),  # ~0.6% steps over a 50%-wide window
        (1, 0.25, 200),  # 0.01% steps: clamped down to the maximum
    ],
)
def test_auto_bins_follow_the_liquidity_granularity(tick_spacing, window_pct, expected):
    pool = _pool(0.02, liquidity=10**21)
    pool.tick_spacing = tick_spacing
    bars = aggregation._auto_num_bins(_prepared(pool), 0.02, 0.02 * (1 - window_pct), 0.02 * (1 + window_pct))
    assert bars == expected


def test_auto_bins_without_active_liquidity_uses_the_maximum():
    pool = _pool(0.02, liquidity=0)
    assert aggregation._auto_num_bins(_prepared(pool), 0.02, 0.015, 0.025) == config.AUTO_BINS_MAX


def test_auto_bins_show_detail_held_by_a_quarter_of_the_liquidity():
    # 30% of the depth in a fine (spacing 8) pool, 70% in a coarse (200)
    # one: the fine pool's detail is worth showing.
    fine, coarse = _pool(0.02, liquidity=3 * 10**20), _pool(0.02, liquidity=7 * 10**20)
    fine.tick_spacing, coarse.tick_spacing = 8, 200
    prepared = _prepared(fine) + _prepared(coarse)
    assert aggregation._auto_num_bins(prepared, 0.02, 0.015, 0.025) == config.AUTO_BINS_MAX
    # ...but not when it's just a sliver (10%).
    fine.liquidity, coarse.liquidity = 10**20, 9 * 10**20
    assert aggregation._auto_num_bins(prepared, 0.02, 0.015, 0.025) == config.AUTO_BINS_MIN

import pytest

from liquidity_depth import visualize
from liquidity_depth.models import AnalysisResult, BinRow, TokenMeta

TOKEN = TokenMeta(address="0x" + "aa" * 20, symbol="TGT", name="Target", decimals=18)


def _result(price: float, heights: list[float]) -> AnalysisResult:
    bins = [BinRow(float(i), float(i + 1), i + 0.5, {"p": h}) for i, h in enumerate(heights)]
    total = sum(heights)
    return AnalysisResult(
        token=TOKEN, current_price_usd=price, price_low=0.0, price_high=float(len(heights)),
        bins=bins, pool_results=[], buyside_usd=total / 2, sellside_usd=total / 2, imbalance_usd=0.0,
    )


def test_depth_curves_start_at_the_price_and_accumulate_outwards():
    # Price in the middle of bin 2: half of that bin counts on each side.
    (bx, by), (sx, sy) = visualize.depth_curves(_result(2.5, [10, 20, 40, 80, 160]))

    assert (bx[0], by[0]) == (2.5, 0.0) and (sx[0], sy[0]) == (2.5, 0.0)
    assert bx == [2.5, 2.0, 1.0, 0.0] and by == [0.0, 20.0, 40.0, 50.0]
    assert sx == [2.5, 3.0, 4.0, 5.0] and sy == [0.0, 20.0, 100.0, 260.0]


def test_depth_curves_add_up_to_every_bar():
    heights = [3.0, 1.0, 4.0, 1.0, 5.0, 9.0, 2.0, 6.0]
    (_, by), (_, sy) = visualize.depth_curves(_result(3.7, heights))
    assert by[-1] + sy[-1] == pytest.approx(sum(heights))
    assert by == sorted(by) and sy == sorted(sy)  # never decreases


def test_figure_has_all_three_views_with_only_one_visible():
    fig = visualize.build_plotly_figure(_result(2.5, [10, 20, 40, 80, 160]), view="cumulative")
    views = {t.meta["view"] for t in fig.data}
    assert views == set(visualize.CHART_VIEWS)
    assert all(t.visible == (t.meta["view"] == "cumulative") for t in fig.data)


def _result_from(price_low: float, width: float, price: float, heights: list[float]) -> AnalysisResult:
    bins = [BinRow(price_low + i * width, price_low + (i + 1) * width, price_low + (i + 0.5) * width, {"p": h})
            for i, h in enumerate(heights)]
    return AnalysisResult(
        token=TOKEN, current_price_usd=price, price_low=price_low, price_high=price_low + len(heights) * width,
        bins=bins, pool_results=[], buyside_usd=0.0, sellside_usd=0.0, imbalance_usd=0.0,
    )


def test_finds_a_resistance_wall_and_a_thinning_support():
    # 20 bars from 0.5 to 1.5, price 1.0 (between bars 9 and 10).
    heights = [60.0] * 4 + [100.0] * 6 + [100.0] * 6 + [130.0] * 4
    steps = visualize.liquidity_steps(_result_from(0.5, 0.05, 1.0, heights))

    assert [(s.side, round(s.price, 4), round(s.change, 2)) for s in steps] == [
        ("buy", 0.7, -0.4),  # below 0.70 there's 40% less liquidity
        ("sell", 1.3, 0.3),  # above 1.30, a wall with 30% more
    ]
    assert steps[1].label == "Resistance wall (+30%)"
    assert steps[0].label == "Thinner below (−40%)"


def test_gradual_liquidity_has_no_steps():
    heights = [100.0 + i for i in range(20)]  # ~1% per bar, never a sharp step
    assert visualize.liquidity_steps(_result_from(0.5, 0.05, 1.0, heights)) == []


def test_steps_far_from_the_price_are_ignored():
    # A big step at 1.8x the price, beyond STEP_MAX_DISTANCE.
    heights = [100.0] * 16 + [300.0] * 4
    assert visualize.liquidity_steps(_result_from(0.5, 0.0825, 1.0, heights)) == []

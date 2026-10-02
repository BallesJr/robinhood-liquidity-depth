"""Stacked-bar liquidity depth chart, in the style of common Uniswap
liquidity depth dashboards: USD price on the x-axis, USD liquidity per bin
on the y-axis, one color per pool, and a vertical line marking the current
price.
"""
from __future__ import annotations

from dataclasses import dataclass

import matplotlib.pyplot as plt
import numpy as np
from matplotlib.ticker import FuncFormatter

from .models import AnalysisResult

# Fixed-order categorical palette (validated: CVD-safe adjacent pairs, >=3:1
# contrast or a documented relief - see the per-pool table below every
# chart, which is that relief). A token can have a dozen-plus dust V4 pools
# (see docs/METHODOLOGY.md "Worked example"); coloring each one individually turns the
# legend into unreadable noise and forces new hues to be generated past the
# point people can tell them apart. Instead only the top few pools by
# window-liquidity get their own color, in this fixed order, and everything
# else is folded into one neutral "Other pools" bucket.
_CATEGORICAL_COLORS = [
    "#2a78d6",  # blue
    "#eb6834",  # orange
    "#1baf7a",  # aqua
    "#eda100",  # yellow
    "#e87ba4",  # magenta
    "#008300",  # green
]
# Same hues stepped for a dark surface (#1a1a19), used by the web UI's dark mode.
_CATEGORICAL_COLORS_DARK = [
    "#3987e5",
    "#d95926",
    "#199e70",
    "#c98500",
    "#d55181",
    "#008300",
]
_OTHER_COLOR = "#898781"  # muted ink - an aggregate bucket, not a pool identity
# Buyside (below the price) / sellside (above), matching the web UI's
# --buy/--sell tokens in light and dark mode.
_BUY_COLOR, _BUY_COLOR_DARK = "#1baf7a", "#199e70"
_SELL_COLOR, _SELL_COLOR_DARK = "#eb6834", "#d95926"
_CURRENT_PRICE_COLOR = "#0b0b0b"
_GRIDLINE_COLOR = "#e1e0d9"
# Labels drawn over bars get a translucent page-colored box so they stay
# readable on any bar color (the web UI swaps in a dark one in dark mode).
_LABEL_BG, _LABEL_BG_DARK = "rgba(252,252,251,0.85)", "rgba(26,26,25,0.85)"
_LABEL_BOX = dict(boxstyle="round,pad=0.25", facecolor="#fcfcfb", edgecolor="none", alpha=0.85)
_TOP_N_POOLS = len(_CATEGORICAL_COLORS)


def _pool_totals(result: AnalysisResult) -> dict[str, float]:
    totals: dict[str, float] = {}
    for b in result.bins:
        for key, usd in b.usd_by_pool.items():
            totals[key] = totals.get(key, 0.0) + usd
    return totals


def _bucket_series(result: AnalysisResult) -> list[tuple[list[str], str, str]]:
    """Group pools into at most `_TOP_N_POOLS` individually-colored series
    (ranked by total USD liquidity in the window) plus one "Other pools"
    bucket for the rest.

    Returns an ordered list of (pool_keys, color, legend_label); a bin's
    height for a given series is the sum of its member pool_keys' USD
    values in that bin.
    """
    totals = _pool_totals(result)
    ordered_keys = sorted(totals, key=lambda k: totals[k], reverse=True)
    top_keys, other_keys = ordered_keys[:_TOP_N_POOLS], ordered_keys[_TOP_N_POOLS:]

    label_by_key = {pr.pool.pool_key: pr.pool.label for pr in result.pool_results}
    series = [([key], _CATEGORICAL_COLORS[i], label_by_key.get(key, key)) for i, key in enumerate(top_keys)]
    if other_keys:
        series.append((other_keys, _OTHER_COLOR, f"Other pools ({len(other_keys)})"))
    return series


def _bin_heights(result: AnalysisResult, keys: list[str]) -> list[float]:
    return [sum(b.usd_by_pool.get(k, 0.0) for k in keys) for b in result.bins]


def depth_curves(result: AnalysisResult) -> tuple[tuple[list[float], list[float]], tuple[list[float], list[float]]]:
    """Cumulative depth from the current price outwards, the way exchange
    depth charts draw an order book: for each price below the current one,
    how much buyside liquidity the price would have to eat through to fall
    that far, and likewise upwards for sellside.

    Returns ((buy_x, buy_y), (sell_x, sell_y)), both starting at
    (current price, 0). Liquidity is taken as spread evenly inside each
    bin, so the bin holding the current price is split proportionally.
    """
    price = result.current_price_usd
    bins = sorted(result.bins, key=lambda b: b.price_low)

    def share(b, lo: float, hi: float) -> float:
        width = b.price_high - b.price_low
        overlap = max(0.0, min(hi, b.price_high) - max(lo, b.price_low))
        return sum(b.usd_by_pool.values()) * (overlap / width if width > 0 else 0.0)

    buy_x, buy_y, total = [price], [0.0], 0.0
    for b in reversed(bins):
        if b.price_low < price:
            total += share(b, b.price_low, price)
            buy_x.append(b.price_low)
            buy_y.append(total)

    sell_x, sell_y, total = [price], [0.0], 0.0
    for b in bins:
        if b.price_high > price:
            total += share(b, price, b.price_high)
            sell_x.append(b.price_high)
            sell_y.append(total)
    return (buy_x, buy_y), (sell_x, sell_y)


# Liquidity steps (see liquidity_steps): a change of at least this much
# between the bars either side of an edge, at most this many per side, only
# within this distance of the current price.
STEP_MIN_CHANGE = 0.10
STEP_MAX_PER_SIDE = 3
STEP_MAX_DISTANCE = 0.5
_STEP_SPAN_BARS = 2


@dataclass(frozen=True)
class LiquidityStep:
    price: float  # the bar edge where liquidity changes
    side: str  # "buy" (below the current price) or "sell" (above)
    change: float  # relative change past the edge, moving away from the price

    @property
    def label(self) -> str:
        if self.side == "sell":
            kind = "Resistance wall" if self.change > 0 else "Thinner above"
        else:
            kind = "Support wall" if self.change > 0 else "Thinner below"
        return f"{kind} ({self.change * 100:+.0f}%)".replace("-", "−")


def liquidity_steps(result: AnalysisResult) -> list[LiquidityStep]:
    """Find the price levels where liquidity changes sharply, which is
    what support and resistance look like in concentrated liquidity: a
    level where many LP ranges start (a wall the price has to eat through)
    or end (thinner liquidity, so the price can move faster past it).

    Walks outwards from the current price on each side and compares the
    _STEP_SPAN_BARS bars just beyond each bar edge with those just before
    it. Short spans keep real steps crisp (on $AI the wall at $0.184 is
    +20% between two adjacent bars, but averaging 4 bars per side diluted
    it below any useful threshold). Returns the biggest changes of at least
    STEP_MIN_CHANGE, at most STEP_MAX_PER_SIDE per side and not closer than
    a span apart, within STEP_MAX_DISTANCE of the current price - far-off
    levels matter less, and near $0 a linear price axis gets distorted.
    Display-only, like the bars it reads.
    """
    price = result.current_price_usd
    bins = sorted(result.bins, key=lambda b: b.price_low)
    heights = [sum(b.usd_by_pool.values()) for b in bins]
    span = _STEP_SPAN_BARS
    steps: list[LiquidityStep] = []

    for side in ("buy", "sell"):
        if side == "buy":
            order = [i for i in reversed(range(len(bins))) if bins[i].price_high <= price]
        else:
            order = [i for i in range(len(bins)) if bins[i].price_low >= price]
        candidates = []
        for j in range(span, len(order) - span + 1):
            near = sum(heights[i] for i in order[j - span : j]) / span
            far = sum(heights[i] for i in order[j : j + span]) / span
            edge = bins[order[j]].price_high if side == "buy" else bins[order[j]].price_low
            if near <= 0 or abs(edge / price - 1) > STEP_MAX_DISTANCE:
                continue
            candidates.append((far / near - 1, edge, j))

        chosen: list[tuple[float, float, int]] = []
        for change, edge, j in sorted(candidates, key=lambda c: abs(c[0]), reverse=True):
            if abs(change) < STEP_MIN_CHANGE or len(chosen) >= STEP_MAX_PER_SIDE:
                break
            if all(abs(j - other) > span for _, _, other in chosen):
                chosen.append((change, edge, j))
        steps += [LiquidityStep(price=edge, side=side, change=change) for change, edge, _ in chosen]

    return sorted(steps, key=lambda st: st.price)


def _rgba(hex_color: str, alpha: float) -> str:
    r, g, b = (int(hex_color[i : i + 2], 16) for i in (1, 3, 5))
    return f"rgba({r},{g},{b},{alpha})"


def _format_usd_axis(value: float, _pos=None) -> str:
    if abs(value) >= 1e6:
        return f"${value / 1e6:g}M"
    if abs(value) >= 1e3:
        return f"${value / 1e3:g}k"
    return f"${value:g}"


def plot_depth_chart(result: AnalysisResult, output_path: str | None = None, show: bool = False, view: str = "total"):
    """Static PNG version of the web chart (used by the CLI). `view` is
    "total" (bars green below / orange above the current price) or "pool"
    (stacked by pool); both mark the liquidity_steps levels. The web UI's
    third, "cumulative" view is interactive-only."""
    x = np.array([b.price_mid for b in result.bins])
    bottom = np.zeros(len(result.bins))

    fig, ax = plt.subplots(figsize=(13, 6.5))
    ax.set_axisbelow(True)
    ax.grid(axis="y", color=_GRIDLINE_COLOR, linewidth=0.8)
    full_bin_width = (result.price_high - result.price_low) / max(len(result.bins), 1)
    bar_width = full_bin_width * 0.85  # leave a visible gap between bars instead of a solid wall of color

    if view == "pool":
        for keys, color, label in _bucket_series(result):
            heights = np.array(_bin_heights(result, keys))
            ax.bar(x, heights, width=bar_width, bottom=bottom, color=color, label=label[:40], edgecolor="none")
            bottom += heights
    else:
        below = x < result.current_price_usd
        heights = np.array([sum(b.usd_by_pool.values()) for b in result.bins])
        ax.bar(x[below], heights[below], width=bar_width, color=_BUY_COLOR, label="Buyside (below price)", edgecolor="none")
        ax.bar(x[~below], heights[~below], width=bar_width, color=_SELL_COLOR, label="Sellside (above price)", edgecolor="none")

    for step in liquidity_steps(result):
        ax.axvline(step.price, color=_OTHER_COLOR, linewidth=1, linestyle=":")
        ax.annotate(
            step.label,
            xy=(step.price, 0.98),
            xycoords=("data", "axes fraction"),
            xytext=(-3 if step.side == "buy" else 3, 0),
            textcoords="offset points",
            rotation=90,
            fontsize=8,
            color=_OTHER_COLOR,
            ha="right" if step.side == "buy" else "left",
            va="top",
            bbox=_LABEL_BOX,
        )

    ax.axvline(result.current_price_usd, color=_CURRENT_PRICE_COLOR, linewidth=1.5, linestyle="--")
    ax.annotate(
        f"Current price\n${result.current_price_usd:,.6g}",
        xy=(result.current_price_usd, 1.0),
        xycoords=("data", "axes fraction"),
        xytext=(6, -4),
        textcoords="offset points",
        fontsize=8,
        color=_CURRENT_PRICE_COLOR,
        va="top",
        bbox=_LABEL_BOX,
    )

    ax.set_xlabel(f"{result.token.symbol} price (USD)")
    ax.set_ylabel("Liquidity per bar (USD)")
    ax.set_title(f"{result.token.symbol} liquidity depth across Uniswap V3/V4 pools - Robinhood Chain")
    ax.set_xlim(result.price_low, result.price_high)
    ax.legend(loc="upper left", bbox_to_anchor=(1.01, 1), fontsize=8, ncol=1, framealpha=0.9)
    ax.yaxis.set_major_formatter(FuncFormatter(_format_usd_axis))
    fig.tight_layout()

    if output_path:
        fig.savefig(output_path, dpi=150)
    if show:
        plt.show()
    plt.close(fig)
    return output_path


CHART_VIEWS = ("total", "pool", "cumulative")
CHART_Y_TITLES = {
    "total": "Liquidity per bar (USD)",
    "pool": "Liquidity per bar (USD)",
    "cumulative": "Cumulative liquidity from current price (USD)",
}


def build_plotly_figure(result: AnalysisResult, view: str = "total"):
    """Same chart as plot_depth_chart, as an interactive Plotly figure for
    the web UI (see webapp/app.py), with three views of the same data:

    - "total": one bar per bin, green below the current price (buyside,
      support) and orange above (sellside, resistance). Steps in height are
      where liquidity starts or ends - the easiest view to spot walls in.
    - "pool": the same bars stacked by pool.
    - "cumulative": exchange-style depth curves (see depth_curves). The
      steeper the curve, the more liquidity sits at that level.

    All traces are in the figure; each carries its view in `meta.view` and
    only `view`'s traces start visible - the page switches views by toggling
    visibility. Drawn with light-mode colors on a transparent background;
    `meta` also carries light and dark colors, which the page's theme script
    swaps in when the viewer is in dark mode.
    """
    import plotly.graph_objects as go

    series = _bucket_series(result)
    x = [b.price_mid for b in result.bins]
    dark_by_light = dict(zip(_CATEGORICAL_COLORS, _CATEGORICAL_COLORS_DARK))
    dark_by_light[_OTHER_COLOR] = _OTHER_COLOR

    fig = go.Figure()
    for keys, color, label in series:
        fig.add_bar(
            x=x,
            y=_bin_heights(result, keys),
            name=label[:48],
            marker_color=color,
            marker_line_width=0,
            visible=view == "pool",
            meta={"view": "pool", "light": color, "dark": dark_by_light.get(color, color)},
            hovertemplate="%{fullData.name}<br>Price $%{x:,.6g}<br><b>$%{y:,.0f}</b><extra></extra>",
        )

    below = [b.price_mid < result.current_price_usd for b in result.bins]
    fig.add_bar(
        x=x,
        y=[sum(b.usd_by_pool.values()) for b in result.bins],
        name="All pools",
        showlegend=False,
        marker_color=[_BUY_COLOR if is_below else _SELL_COLOR for is_below in below],
        marker_line_width=0,
        visible=view == "total",
        meta={
            "view": "total",
            "light": [_BUY_COLOR if is_below else _SELL_COLOR for is_below in below],
            "dark": [_BUY_COLOR_DARK if is_below else _SELL_COLOR_DARK for is_below in below],
        },
        hovertemplate="Price $%{x:,.6g}<br><b>$%{y:,.0f}</b><extra></extra>",
    )

    (buy_x, buy_y), (sell_x, sell_y) = depth_curves(result)
    for xs, ys, name, light, dark, verb in (
        (buy_x, buy_y, "Buyside (support)", _BUY_COLOR, _BUY_COLOR_DARK, "Down to"),
        (sell_x, sell_y, "Sellside (resistance)", _SELL_COLOR, _SELL_COLOR_DARK, "Up to"),
    ):
        fig.add_scatter(
            x=xs,
            y=ys,
            name=name,
            mode="lines",
            line=dict(color=light, width=2),
            fill="tozeroy",
            fillcolor=_rgba(light, 0.18),
            visible=view == "cumulative",
            meta={"view": "cumulative", "light": light, "dark": dark,
                  "fill_light": _rgba(light, 0.18), "fill_dark": _rgba(dark, 0.22)},
            hovertemplate=f"{verb} $%{{x:,.6g}}<br><b>$%{{y:,.0f}}</b> of {name.split()[0].lower()}<extra></extra>",
        )

    fig.add_vline(
        x=result.current_price_usd,
        line_dash="dash",
        line_color=_CURRENT_PRICE_COLOR,
        line_width=1.5,
        annotation_text=f"Current price ${result.current_price_usd:,.6g}",
        annotation_position="top right",
        annotation_font_size=12,
        annotation_font_color=_CURRENT_PRICE_COLOR,
        annotation_bgcolor=_LABEL_BG,
    )
    # Liquidity steps as thin dotted lines in every view, labelled at the top.
    # Added after the current-price line so that stays shapes[0] /
    # annotations[0] for the page's theme script.
    for step in liquidity_steps(result):
        fig.add_vline(
            x=step.price,
            line_dash="dot",
            line_color=_OTHER_COLOR,
            line_width=1,
            annotation_text=step.label,
            annotation_position="top left" if step.side == "buy" else "top right",
            annotation_textangle=-90,
            annotation_yanchor="top",
            annotation_font_size=11,
            annotation_font_color=_OTHER_COLOR,
            annotation_bgcolor=_LABEL_BG,
        )
    fig.update_layout(
        barmode="stack",
        bargap=0.15,
        xaxis=dict(title=f"{result.token.symbol} price (USD)", range=[result.price_low, result.price_high],
                   tickprefix="$", showgrid=False, linecolor="#c3c2b7"),
        yaxis=dict(title=CHART_Y_TITLES[view], tickprefix="$", tickformat="~s", gridcolor=_GRIDLINE_COLOR,
                   zeroline=False),
        template="plotly_white",
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(family='system-ui, -apple-system, "Segoe UI", sans-serif', size=12, color="#52514e"),
        height=520,
        margin=dict(t=40, r=16, b=48, l=64),
        legend=dict(orientation="h", yanchor="bottom", y=1.04, x=0, font=dict(size=12)),
        hovermode="closest",
        hoverlabel=dict(font_size=12),
    )
    return fig

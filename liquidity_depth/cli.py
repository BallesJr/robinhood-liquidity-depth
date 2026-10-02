"""Command-line entry point.

Example:
    python -m liquidity_depth.cli --token 0x2e8C31162B855a2fFa90F6F8634643aD6F111E18 --window 0.5
"""
from __future__ import annotations

import argparse
import logging
import math
import sys

from . import config
from .aggregation import analyze_token
from .chain import get_w3
from .models import AnalysisResult
from .rpc_utils import call_with_retry
from .visualize import liquidity_steps, plot_depth_chart

logger = logging.getLogger("liquidity_depth")


def _format_usd(value: float) -> str:
    return f"${value:,.2f}"


def _format_price(value: float) -> str:
    """Token prices need more precision than USD totals: a $0.0451
    memecoin would print as "$0.05" with 2 decimals. Shows about 4
    significant digits below $1."""
    if value <= 0 or value >= 1:
        return _format_usd(value)
    decimals = -math.floor(math.log10(value)) + 3
    return f"${value:.{decimals}f}"


def _print_summary(result: AnalysisResult) -> None:
    print()
    print(f"=== {result.token.symbol} ({result.token.name}) liquidity depth on Robinhood Chain ===")
    print(f"Current price:     {_format_price(result.current_price_usd)}")
    print(f"Window:            {_format_price(result.price_low)} - {_format_price(result.price_high)}")
    print(f"Buyside $:         {_format_usd(result.buyside_usd)}")
    print(f"Sellside $:        {_format_usd(result.sellside_usd)}")
    sign = "+" if result.imbalance_usd >= 0 else "-"
    total = result.buyside_usd + result.sellside_usd
    buy_pct = result.buyside_usd / total * 100 if total > 0 else 50.0
    print(f"Imbalance $:       {sign}{_format_usd(abs(result.imbalance_usd))} ({buy_pct:.0f}% buyside)")

    pc = result.price_check
    if pc is None:
        pass
    elif not pc.available:
        print("Price cross-check: unavailable (no DexScreener data for this token)")
    else:
        flag = " *** DIVERGES from independent price source ***" if pc.diverged else ""
        print(f"Price cross-check: {pc.source} ${pc.external_price_usd:,.6g} (diff {pc.diff_pct:.1f}%){flag}")
    levels = liquidity_steps(result)
    # Plain "-" here: the label's typographic minus can't be printed on a
    # default Windows console (cp1252).
    print("Key levels:        " + ("; ".join(f"{_format_price(st.price)} {st.label.replace('−', '-')}" for st in levels)
                                  or "none (liquidity changes gradually)"))
    print()

    header = f"{'Pair':<22}{'Ver':<5}{'Fee':<10}{'Pool / PoolId':<44}{'TVL (USD)':>14}  {'Liquidity in window (USD)':>26}"
    print(header)
    print("-" * len(header))
    rows = sorted(result.pool_results, key=lambda r: r.liquidity_in_window_usd, reverse=True)
    for r in rows:
        pair = f"{r.pool.token0.symbol}/{r.pool.token1.symbol}"
        tvl = _format_usd(r.pool.tvl_usd) if r.pool.tvl_usd is not None else "n/a"
        window_liq = _format_usd(r.liquidity_in_window_usd) if r.priceable else "skipped (no USD price)"
        print(f"{pair:<22}{r.pool.version.upper():<5}{r.pool.fee_display:<10}{r.pool.pool_key:<44}{tvl:>14}  {window_liq:>26}")
    print()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Uniswap V3/V4 liquidity depth for a token on Robinhood Chain")
    parser.add_argument("--token", default=config.DEFAULT_TOKEN, help="ERC-20 token address to analyze")
    parser.add_argument("--window", type=float, default=config.DEFAULT_WINDOW_PCT, help="Price window as a fraction, e.g. 0.5 = +/-50%%")
    parser.add_argument("--bins", type=int, default=config.DEFAULT_NUM_BINS, help="Number of chart bars (default: picked automatically per token)")
    parser.add_argument("--output", default="liquidity_depth.png", help="Path to save the chart PNG")
    parser.add_argument("--no-chart", action="store_true", help="Skip generating the chart, print summary only")
    parser.add_argument("--no-cache", action="store_true", help="Ignore the on-disk V4 pool cache and rescan from block 0")
    parser.add_argument("--show", action="store_true", help="Display the chart in a window")
    parser.add_argument("--view", choices=["total", "pool"], default="total",
                        help="Chart style: total bars colored by side (default) or stacked by pool")
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose logging")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="[%(levelname)s] %(message)s")

    w3 = get_w3()
    block_number = call_with_retry(lambda: w3.eth.block_number)
    print(f"Connected to Robinhood Chain (chain id {w3.eth.chain_id}), block {block_number}")

    try:
        result = analyze_token(w3, args.token, window_pct=args.window, num_bins=args.bins, use_cache=not args.no_cache)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    _print_summary(result)

    if not args.no_chart:
        path = plot_depth_chart(result, output_path=args.output, show=args.show, view=args.view)
        print(f"Chart saved to {path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

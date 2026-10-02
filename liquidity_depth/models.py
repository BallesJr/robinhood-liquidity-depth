"""Shared data structures passed between modules."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TokenMeta:
    address: str
    symbol: str
    name: str
    decimals: int


@dataclass
class PoolInfo:
    """A single Uniswap V3 or V4 pool, after its current state has been read."""

    version: str  # "v3" or "v4"
    pool_key: str  # pool contract address (v3) or 0x poolId (v4) - used as a unique key
    fee: int  # hundredths of a bip, e.g. 3000 == 0.30%
    tick_spacing: int
    hooks: str | None  # v4 only, None for v3
    token0: TokenMeta
    token1: TokenMeta
    sqrt_price_x96: int
    tick: int
    liquidity: int  # current in-range liquidity
    tvl_usd: float | None = None  # only populated for v3 (see pools_v3.compute_tvl_usd)

    @property
    def fee_display(self) -> str:
        """Fee tier as a human string; V4 hook pools can set fees
        dynamically at swap time, signaled by LPFeeLibrary.DYNAMIC_FEE_FLAG
        (0x800000) instead of a real fee value.
        """
        if self.fee == 0x800000:
            return "dynamic"
        return f"{self.fee / 10_000:.2f}%"

    @property
    def label(self) -> str:
        proto = self.version.upper()
        return f"{self.token0.symbol}/{self.token1.symbol} {self.fee_display} ({proto})"


@dataclass
class BinRow:
    price_low: float
    price_high: float
    price_mid: float
    usd_by_pool: dict[str, float]  # pool_key -> USD liquidity value in this bin

    @property
    def total_usd(self) -> float:
        return sum(self.usd_by_pool.values())


@dataclass
class PoolResult:
    """A pool after tick data has been fetched and valued in USD."""

    pool: PoolInfo
    quote_token: TokenMeta
    target_is_token0: bool
    current_target_price_usd: float | None
    liquidity_in_window_usd: float
    priceable: bool
    skip_reason: str | None = None


@dataclass
class PriceCheck:
    """Result of comparing the on-chain-reconstructed price against an
    independent external source (see price_check.py).

    `available=False` means the external source simply had no data for this
    token (common - Robinhood Chain is new/small) - that is "unverified",
    not evidence of a problem, and should be presented differently in the
    UI than a real divergence.
    """

    available: bool
    source: str
    external_price_usd: float | None
    diff_pct: float | None
    diverged: bool


@dataclass
class AnalysisResult:
    token: TokenMeta
    current_price_usd: float
    price_low: float
    price_high: float
    bins: list[BinRow]
    pool_results: list[PoolResult]
    buyside_usd: float
    sellside_usd: float
    imbalance_usd: float
    price_check: PriceCheck | None = None

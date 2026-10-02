"""Central configuration for the Robinhood Chain liquidity depth tool.

Every contract/token address below was independently verified on-chain
against Robinhood Chain (chain ID 4663) before being hardcoded here -
see docs/METHODOLOGY.md ("Address verification") for exactly how each one was
checked. Nothing here was taken on faith from search results or docs
pages alone.

All values can be overridden via environment variables - see .env.example.
"""
from __future__ import annotations

import os

from dotenv import load_dotenv
from web3 import Web3

load_dotenv()


def _addr(value: str) -> str:
    return Web3.to_checksum_address(value)


CHAIN_ID = 4663
RPC_URL = os.getenv("RPC_URL", "https://rpc.mainnet.chain.robinhood.com")
BLOCKSCOUT_URL = os.getenv("BLOCKSCOUT_URL", "https://robinhoodchain.blockscout.com")

# --- Verified core Uniswap contracts on Robinhood Chain --------------------
# UniswapV3Factory: verified by calling feeAmountTickSpacing() for all four
# standard fee tiers (100/500/3000/10000) and observing the exact expected
# tick spacings (1/10/60/200), plus confirming live PoolCreated events.
UNISWAP_V3_FACTORY = _addr(os.getenv("UNISWAP_V3_FACTORY", "0x1f7d7550b1B028F7571E69a784071F0205Fd2EfA"))

# Uniswap V4 PoolManager singleton: verified via hundreds of live Initialize
# events on-chain, a successful protocolFeeController() call, and matching
# runtime bytecode length against the official Base deployment.
UNISWAP_V4_POOL_MANAGER = _addr(os.getenv("UNISWAP_V4_POOL_MANAGER", "0x8366A39CC670B4001A1121b8f6A443A643E40951"))

STANDARD_V3_FEE_TIERS: tuple[int, ...] = (100, 500, 3000, 10000)

# Multicall3 (canonical deterministic deployment): verified by runtime
# bytecode (3808 bytes, keccak 0xd5c15df6...0891, identical to every other
# chain's canonical Multicall3), getChainId() returning 4663, and
# aggregate3() returning byte-identical results to direct calls for 1,080
# V3 ticks()/V4 extsload reads. Set to an empty string to disable it and
# fall back to plain JSON-RPC batching (see rpc_utils.batch_eth_call).
MULTICALL3_ADDRESS = os.getenv("MULTICALL3_ADDRESS", "0xcA11bde05977b3631167028862bE2a173976CA11")
MULTICALL3_ADDRESS = _addr(MULTICALL3_ADDRESS) if MULTICALL3_ADDRESS else ""

# --- Verified tokens ---------------------------------------------------
# Native ETH sentinel used by Uniswap V4 (currency == address(0) means the
# pool holds native ETH directly rather than wrapped WETH).
NATIVE_ETH_SENTINEL = "0x0000000000000000000000000000000000000000"

WETH = _addr(os.getenv("WETH_ADDRESS", "0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73"))
USDG = _addr(os.getenv("USDG_ADDRESS", "0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168"))
NVDA = _addr(os.getenv("NVDA_ADDRESS", "0xd0601CE157Db5bdC3162BbaC2a2C8aF5320D9EEC"))

# Example/demo token used when no --token argument is given on the CLI.
# Artificial Inu ($AI) - verified on-chain via name()/symbol()/decimals().
DEFAULT_TOKEN = _addr(os.getenv("DEFAULT_TOKEN", "0x2e8C31162B855a2fFa90F6F8634643aD6F111E18"))


def _parse_quote_tokens() -> list[str]:
    raw = os.getenv("QUOTE_TOKENS")
    if not raw:
        return [WETH, USDG, NVDA]
    return [_addr(a.strip()) for a in raw.split(",") if a.strip()]


# Tokens the target token is checked against when discovering pools.
QUOTE_TOKENS: list[str] = _parse_quote_tokens()

# --- Depth chart parameters ---------------------------------------------
DEFAULT_WINDOW_PCT = float(os.getenv("DEFAULT_WINDOW_PCT", "0.5"))
# Number of chart bars. Unset (the default) = pick automatically per token
# from how fine its liquidity is (aggregation._auto_num_bins), within
# AUTO_BINS_MIN..AUTO_BINS_MAX. Set it to force a fixed count. Display-only:
# buyside/sellside/imbalance don't depend on it.
DEFAULT_NUM_BINS: int | None = int(os.environ["DEFAULT_NUM_BINS"]) if os.getenv("DEFAULT_NUM_BINS") else None
AUTO_BINS_MIN = int(os.getenv("AUTO_BINS_MIN", "40"))
AUTO_BINS_MAX = int(os.getenv("AUTO_BINS_MAX", "200"))

# Safety cap on how many V4 pools get the (expensive) full tick-range
# extraction, after cheap liquidity-based filtering. V4's PoolManager is a
# singleton shared by every pool on the chain, so a popular token can easily
# have thousands of Initialize events, most from long-dead/dust pools.
MAX_V4_POOLS = int(os.getenv("MAX_V4_POOLS", "15"))

# Concurrency used for batches of independent read-only RPC calls
# (tick bitmap words, ticks()/extsload lookups, etc).
RPC_CONCURRENCY = int(os.getenv("RPC_CONCURRENCY", "6"))

# JSON-RPC batch size for bulk eth_call requests (e.g. checking liquidity
# across thousands of V4 pool candidates). Measured empirically against the
# public RPC: 48 already caused retries under load, and 60+ gets rejected
# outright - see docs/METHODOLOGY.md "Performance notes". With Multicall3
# (below) each entry in a batch is a whole aggregate3() of many calls.
RPC_BATCH_SIZE = int(os.getenv("RPC_BATCH_SIZE", "35"))

# How many calls rpc_utils.batch_eth_call packs into one Multicall3
# aggregate3() eth_call. The RPC's rate limit counts eth_calls (even inside
# a JSON-RPC batch), not the work each one does: one aggregate3 of 500
# ticks() reads costs about the same ~1s as a single plain call, while 500
# plain calls cost ~15 sequential batches. Larger aggregates get slower
# (3,000 storage reads in one call took ~6s), so 500 is the sweet spot.
MULTICALL_CHUNK_SIZE = int(os.getenv("MULTICALL_CHUNK_SIZE", "500"))

RPC_TIMEOUT_SECONDS = int(os.getenv("RPC_TIMEOUT_SECONDS", "30"))

# --- Independent price cross-check (price_check.py) ----------------------
# DexScreener's chain slug for Robinhood Chain - confirmed empirically via
# https://api.dexscreener.com/latest/dex/tokens/<any Robinhood Chain token>,
# which returns pairs with "chainId": "robinhood".
DEXSCREENER_CHAIN_ID = os.getenv("DEXSCREENER_CHAIN_ID", "robinhood")

# Flag the computed price in the UI/CLI if it differs from the DexScreener
# price by more than this many percent.
PRICE_CHECK_THRESHOLD_PCT = float(os.getenv("PRICE_CHECK_THRESHOLD_PCT", "10"))

# --- Web UI result cache (webapp/app.py) ----------------------------------
# How long a full analysis result is reused for repeated requests with the
# same token/window/bins, so clicking "Analyze" again (or a page reload)
# doesn't redo the whole pipeline. The "Refresh" button always
# bypasses this and fetches live. Deliberately short - block times are
# ~100ms on this chain, so anything older than this is already stale.
WEB_RESULT_CACHE_TTL_SECONDS = int(os.getenv("WEB_RESULT_CACHE_TTL_SECONDS", "45"))

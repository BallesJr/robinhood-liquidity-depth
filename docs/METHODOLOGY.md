# Methodology

How the depth chart and the imbalance metric are computed, what they do
and don't tell you, and the engineering choices behind them.

- [Pipeline](#pipeline)
- [Limitations](#limitations)
- [Price cross-check](#price-cross-check)
- [Address verification](#address-verification)
- [Performance notes](#performance-notes)
- [Worked example](#worked-example)

## Pipeline

For a given token address:

1. **Discover pools.** V3 pools are found via the factory's `getPool()` for
   every (quote token, fee tier) pair. V4 pools are found from the
   PoolManager singleton's `Initialize` events, filtered by the token's
   address as either `currency0` or `currency1`.
2. **Read state.** Each pool's current price and its initialized ticks
   within a `±window` band around the current price are read on-chain.
   The window is centred on the median of the pools' prices, weighted by
   each pool's active liquidity. A pool with no active liquidity (its LPs
   have left) keeps the price of its last trade, which can be long stale,
   so it gets no weight. The reported current price is the average of the
   pools' prices, weighted the same way.
3. **Reconstruct liquidity.** Active liquidity per price segment is rebuilt
   from the tick data and converted to token amounts using the standard
   Uniswap V3 whitepaper formulas.
4. **Value in USD.** Each segment is valued at the current USD price of
   both tokens.
5. **Aggregate.** Segments from every pool are bucketed into a shared set
   of USD-price bins and stacked by pool, which gives the chart. A segment
   that spans several bins is cut at the bin edges and each piece goes to
   its own bin, so a wide position is drawn spread out rather than as one
   spike. By default the number of bins follows how fine the token's
   liquidity is: each bin is about one tick-spacing step of the pools that
   hold at least a quarter of the active liquidity, between 40 and 200
   bins. `--bins` (CLI) or the Bins field (web) overrides it.
6. **Imbalance.** Buyside $ is the liquidity below the current price,
   sellside $ the liquidity above it, and imbalance $ = buyside − sellside.
   This is computed per segment rather than per bin, so it doesn't depend
   on the number of bins chosen for display. Each pool's segments are also
   split at the current tick, so the segment holding the live price never
   straddles it. Otherwise a wide position (full-range, or a V4 pool with a
   very large tick spacing) would be counted entirely on one side.

## Limitations

A positive imbalance means there is more concentrated liquidity waiting
below the current price than above it. It is **not** an order book, and
it should not be read as one:

- **Concentrated liquidity is not a resting order.** A V3/V4 position is
  a standing offer to trade *within* its range along the pool's bonding
  curve. It is not a limit order at a fixed price.
- **It doesn't predict price direction.** Liquidity concentration reflects
  where LPs *expect* the price to move, or where it has already moved
  from. It doesn't say where the price is going next.
- **Both legs are valued at the current spot price**, not at the
  execution price inside each range. Most Uniswap depth dashboards do the
  same. A swap large enough to move through a bin would realize a
  different blended price.
- **V4 hook pools are treated as standard concentrated liquidity.** A pool
  with a non-zero `hooks` address can run arbitrary custom logic (dynamic
  fees, custom curves) that this tool cannot detect or model.
- **USD pricing depends on a small set of quote-token feeds.** USDG is
  assumed to hold its peg; WETH and NVDA come from external feeds (see
  `liquidity_depth/tokens.py`). Pools quoted in any other token appear in
  the pool table but are excluded from the chart and imbalance.
- **V4 TVL is not shown.** V3 pools hold their own reserves, so TVL is a
  direct `balanceOf()` read. V4's PoolManager holds every pool's reserves
  together, so there is no per-pool balance to read. The table shows
  `n/a` for V4 and reports "liquidity in window" instead.

## Price cross-check

The computed price depends entirely on on-chain pool state plus the
quote-token USD feeds. Nothing in the pipeline guarantees it is correct:
a quote-token price can be stale, a thin or mispriced pool can skew the
median, or there can simply be a bug.

As a sanity check, `liquidity_depth/price_check.py` looks the token up on
[DexScreener](https://dexscreener.com) (keyless, and it indexes Robinhood
Chain as `"robinhood"`). It then compares the price of DexScreener's
highest-liquidity pair against the computed one. Only pairs where the
token is the *base* token count, because DexScreener's `priceUsd` is always
the base token's price.

- If DexScreener has no data for the token, the check is reported as
  "unavailable", not as a divergence.
- If the two prices differ by more than `PRICE_CHECK_THRESHOLD_PCT`
  (default 10%), the CLI prints a warning and the web UI shows a banner.
- A failed lookup never breaks the rest of the analysis.

## Address verification

Every contract and token address in `liquidity_depth/config.py` was
verified **on-chain**, not just cross-referenced against documentation:

| Address | How it was verified |
|---|---|
| RPC / chain ID | `eth_chainId` returns `0x1237` (4663) |
| Uniswap V3 Factory | `feeAmountTickSpacing()` returns the expected spacing for all four standard fee tiers (100→1, 500→10, 3000→60, 10000→200); live `PoolCreated` events |
| Uniswap V4 PoolManager | Hundreds of live `Initialize` events; `protocolFeeController()` responds; runtime bytecode length matches the official Base deployment |
| WETH | Matches Robinhood's own [contracts page](https://docs.robinhood.com/chain/contracts/) |
| USDG | `name()`/`symbol()`/`decimals()` → `"Global Dollar"` / `"USDG"` / `6` |
| NVDA | `name()`/`symbol()`/`decimals()` → `"NVIDIA x Robinhood Token"` / `"NVDA"` / `18` |
| Multicall3 | Runtime bytecode (3,808 bytes) hashes to the canonical Multicall3 code hash; `getChainId()` returns 4663; `aggregate3()` returned byte-identical results to direct calls for 1,080 V3 `ticks()` / V4 `extsload` reads |
| Artificial Inu (AI) | `name()`/`symbol()`/`decimals()` → `"Artificial Inu"` / `"AI"` / `18` |

To avoid depending on addresses that could not be verified this way, the
tool doesn't use Uniswap's periphery helper contracts:

- V3 tick data is read directly from each pool's `tickBitmap()`/`ticks()`,
  so it doesn't need TickLens.
- V4 state is read with `extsload` directly against the PoolManager, using
  the storage layout from Uniswap's `StateLibrary.sol`/`Pool.sol`/`Slot0.sol`,
  so it doesn't need StateView.

Multicall3 is used only to *transport* those same calls (see
[Performance notes](#performance-notes)); it never changes what is read.
If it is unavailable, calls fall back to plain batching, and setting
`MULTICALL3_ADDRESS=` (empty) turns it off entirely.

## Performance notes

A typical analysis takes about 10–15 seconds against the public RPC. Most
of the design is shaped by these properties of Robinhood Chain and its
RPC endpoint.

**The RPC limits eth_calls, not work.** The public RPC counts every
`eth_call`, including each call inside a JSON-RPC batch. A batch of 35
calls takes about 1 s, and sending two such batches in parallel gets HTTP
429 responses. What a single call does barely matters, though: one
Multicall3 `aggregate3()` wrapping 500 `ticks()` or `extsload` reads also
takes about 1 s. So `rpc_utils.batch_eth_call` packs calls 500 at a time
(`MULTICALL_CHUNK_SIZE`) into `aggregate3()` calls, sends those with
JSON-RPC batching (`RPC_BATCH_SIZE`, default 35), and backs off
exponentially on rate limits (`call_with_retry`). For $AI this cut a run
from about 1,230 `eth_call`s (~48 s) to 11 (~12 s), with identical results.
Under load, larger JSON-RPC batches (e.g. 48) caused enough retries to make
runs *slower*. If you see retry warnings, lower `RPC_BATCH_SIZE` rather
than raising it.

**Round-trip latency dominates.** Anything that ran once per pool was
restructured into one batched round trip for all pools. This covers
state refresh (`refresh_states_batch`), tick fetches
(`fetch_initialized_ticks_batch`), V3 `getPool()` lookups and token
metadata. As a result, roughly 80 round trips across ~20 pools became
about 4. The V3 and V4 halves of each step then run in parallel: with
Multicall3 each is usually a single `eth_call`, well within the rate
limit.

**Blocks are ~100 ms apart.** A lot of trading can happen between two RPC
calls. Early versions read a pool's state at discovery time and its ticks
later, and got impossible results (reserves larger than the pool's actual
balance). Pool state is therefore always re-read immediately before its
ticks are fetched.

**V4 discovery is noisy.** An established token can have thousands of V4
`Initialize` events, most of them dust pools paired with one-off spam
tokens. Two things keep this cheap:

- *Known-quote-first filtering.* `Initialize` events include both
  currencies at no extra cost. Candidates quoted in a priceable token
  (usually a few hundred) are liquidity-checked first. If those already
  fill `MAX_V4_POOLS`, the spam tail is never checked.
- *Persistent candidate cache* (`pool_cache.py`, `.cache/`). Pools are
  never deleted, so the candidate list is cached on disk together with
  the last block scanned. Later runs only scan newer blocks. Only the
  candidate *list* is cached; liquidity and prices are always read live.
  Pass `--no-cache` to force a full rescan.

**No archive state.** The public RPC can't serve `eth_call` at old blocks,
so pool discovery relies on `eth_getLogs`. Each request is capped at 10,000
matching logs and a span of 10 million blocks;
`rpc_utils.get_logs_with_fallback` splits the range in half automatically
whenever either cap is hit.

The V4 liquidity check across every known-quote candidate still runs on
every analysis, so a pool that recently became active is never missed.
With Multicall3 it costs about one request (~1 s) instead of ~15.

The web UI keeps results in memory for a short time
(`WEB_RESULT_CACHE_TTL_SECONDS`, default 45 s), so repeating the same query
is instant. The **Refresh** button always bypasses this cache.

## Worked example

Artificial Inu ($AI, `0x2e8C31162B855a2fFa90F6F8634643aD6F111E18`) with a
±50% window had:

- **V3:** 6 pools, against WETH, USDG and NVDA.
- **V4:** 14,204 `Initialize` events (as of October 2026). Of these, 442
  were quoted in a priceable token: 237 USDG, 187 native ETH, 13 NVDA and
  5 WETH. The rest were unpriceable meme-token pairs.

The largest pools by USD liquidity were V4 pools (AI/NVDA and AI/USDG with
dynamic fees, plus several AI/ETH pools), on par with or ahead of the
biggest V3 pool. This is why V4 can't be skipped even though it's the more
expensive side to discover.

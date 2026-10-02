"""Independent price cross-check against DexScreener.

The price this tool reports is reconstructed entirely from raw on-chain pool
state plus a small set of quote-token price feeds (see tokens.py) - nothing
confirms that math, that feed, or the pool picked as the "current price"
driver is actually right. This module is a cheap sanity check: look up the
same token on DexScreener (a public, keyless API that already indexes
Robinhood Chain under the "robinhood" chain slug) and flag it if the two
disagree by more than `config.PRICE_CHECK_THRESHOLD_PCT`.

Best-effort by design and never raises: DexScreener may simply have no data
for a given token (small/new chain, brand-new pool), and that is reported as
"unavailable", never as a divergence.
"""
from __future__ import annotations

import logging

import requests

from . import config
from .models import PriceCheck

logger = logging.getLogger("liquidity_depth")

_DEXSCREENER_URL = "https://api.dexscreener.com/latest/dex/tokens/{address}"


def _fetch_external_price_usd(token_address: str) -> float | None:
    try:
        resp = requests.get(_DEXSCREENER_URL.format(address=token_address), timeout=8)
        resp.raise_for_status()
        pairs = resp.json().get("pairs") or []
    except Exception as exc:  # noqa: BLE001 - external API, best-effort only
        logger.debug("DexScreener price check unavailable for %s: %s", token_address, exc)
        return None

    # Several pairs can exist per token (different quote tokens, different
    # DEXes, dust pools) - the highest-liquidity one is the most reliable.
    # Only pairs where the token is the *base*: DexScreener's priceUsd is
    # always the base token's price, and this endpoint also returns pairs
    # where our token is merely the quote (e.g. for BONER, its top pair was
    # HIMS/BONER, whose $29.83 priceUsd is HIMS's price, not BONER's).
    chain_pairs = [
        p
        for p in pairs
        if p.get("chainId") == config.DEXSCREENER_CHAIN_ID
        and p.get("priceUsd")
        and str((p.get("baseToken") or {}).get("address", "")).lower() == token_address.lower()
    ]
    if not chain_pairs:
        return None

    best = max(chain_pairs, key=lambda p: (p.get("liquidity") or {}).get("usd") or 0.0)
    try:
        price = float(best["priceUsd"])
    except (TypeError, ValueError):
        return None
    return price if price > 0 else None


def check_price(token_address: str, computed_price_usd: float) -> PriceCheck:
    """Compare `computed_price_usd` (our reconstructed price) against
    DexScreener. Never raises - a failed or empty lookup comes back as
    `available=False` rather than propagating an error.
    """
    external = _fetch_external_price_usd(token_address)
    if external is None:
        return PriceCheck(available=False, source="DexScreener", external_price_usd=None, diff_pct=None, diverged=False)

    diff_pct = abs(computed_price_usd - external) / external * 100
    return PriceCheck(
        available=True,
        source="DexScreener",
        external_price_usd=external,
        diff_pct=diff_pct,
        diverged=diff_pct > config.PRICE_CHECK_THRESHOLD_PCT,
    )

"""On-disk cache for V4 pool *discovery* results, keyed by token address.

Only the discovery output is cached: which PoolIds exist for a token, and
the block range already scanned for `Initialize` events. Live state
(liquidity, price, TVL) is NEVER cached - it changes every block and is
always re-read fresh (see pools_v3.refresh_state / pools_v4.refresh_state).

Because Initialize events are append-only (a V4 pool, once created, is
never removed), a cached candidate list is always safe to reuse - a repeat
run only needs to scan blocks *after* the last cached block to pick up any
pools created since.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger("liquidity_depth")

CACHE_DIR = Path(__file__).resolve().parent.parent / ".cache"

# Bump whenever an older cache can't be trusted, so it's rescanned instead
# of reused. v2: v1 caches were built with Initialize topic filters shifted
# by one position, and so are missing every pool where the token is
# currency1 (see pools_v4.discover_v4_pools).
CACHE_VERSION = 2


def _cache_path(token_address: str) -> Path:
    return CACHE_DIR / f"v4_pools_{token_address.lower()}.json"


def load(token_address: str) -> dict | None:
    path = _cache_path(token_address)
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Could not read pool cache %s: %s", path, exc)
        return None
    if data.get("version") != CACHE_VERSION:
        logger.info("Pool cache %s is from an older version; rescanning", path.name)
        return None
    return data


def save(token_address: str, last_block: int, candidates: dict[str, dict]) -> None:
    CACHE_DIR.mkdir(exist_ok=True)
    path = _cache_path(token_address)
    try:
        with path.open("w", encoding="utf-8") as f:
            json.dump({"version": CACHE_VERSION, "last_block": last_block, "candidates": candidates}, f)
    except OSError as exc:
        logger.warning("Could not write pool cache %s: %s", path, exc)

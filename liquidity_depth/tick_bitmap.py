"""Tick <-> tick-bitmap word/bit conversions.

This mirrors Uniswap's TickBitmap.sol exactly (the compress()/position()
functions, shared byte-for-byte between the V3 and V4 core contracts):
Python's native `//` and `>>` operators already floor towards negative
infinity for ints, which is exactly the rounding Solidity's assembly
implementation goes out of its way to emulate - so the port is direct.
"""
from __future__ import annotations


def compress_tick(tick: int, tick_spacing: int) -> int:
    """Tick index divided by spacing, floored towards negative infinity."""
    return tick // tick_spacing


def word_and_bit_pos(compressed_tick: int) -> tuple[int, int]:
    word_pos = compressed_tick >> 8
    bit_pos = compressed_tick & 0xFF
    return word_pos, bit_pos


def tick_from_word_bit(word_pos: int, bit_pos: int, tick_spacing: int) -> int:
    compressed = (word_pos << 8) + bit_pos
    return compressed * tick_spacing


def word_range_for_ticks(tick_lo: int, tick_hi: int, tick_spacing: int) -> range:
    """Inclusive range of tickBitmap word indices covering [tick_lo, tick_hi]."""
    w_lo, _ = word_and_bit_pos(compress_tick(tick_lo, tick_spacing))
    w_hi, _ = word_and_bit_pos(compress_tick(tick_hi, tick_spacing))
    return range(w_lo, w_hi + 1)


def initialized_ticks_in_word(word_pos: int, bitmap: int, tick_spacing: int) -> list[int]:
    """Decode a tickBitmap word into the list of initialized tick indices it covers."""
    if bitmap == 0:
        return []
    ticks = []
    for bit_pos in range(256):
        if (bitmap >> bit_pos) & 1:
            ticks.append(tick_from_word_bit(word_pos, bit_pos, tick_spacing))
    return ticks

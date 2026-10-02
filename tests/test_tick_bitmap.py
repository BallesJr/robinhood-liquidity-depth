from liquidity_depth import tick_bitmap as tb


def test_compress_floors_towards_negative_infinity():
    # Matches TickBitmap.sol: -1 // 60 must be -1, not 0.
    assert tb.compress_tick(-1, 60) == -1
    assert tb.compress_tick(-60, 60) == -1
    assert tb.compress_tick(59, 60) == 0


def test_word_and_bit_round_trip():
    for tick in (-887220, -12000, -60, 0, 60, 123480, 887220):
        word, bit = tb.word_and_bit_pos(tb.compress_tick(tick, 60))
        assert 0 <= bit < 256
        assert tb.tick_from_word_bit(word, bit, 60) == tick


def test_initialized_ticks_in_word():
    bitmap = (1 << 0) | (1 << 5) | (1 << 255)
    assert tb.initialized_ticks_in_word(-1, bitmap, 10) == [-2560, -2510, -10]
    assert tb.initialized_ticks_in_word(3, 0, 10) == []


def test_word_range_covers_both_ends():
    words = tb.word_range_for_ticks(-30000, 30000, 60)
    assert words.start == tb.word_and_bit_pos(tb.compress_tick(-30000, 60))[0]
    assert words.stop - 1 == tb.word_and_bit_pos(tb.compress_tick(30000, 60))[0]

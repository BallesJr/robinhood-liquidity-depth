from unittest import mock

import pytest

from liquidity_depth import config, tokens


@pytest.fixture(autouse=True)
def clear_price_cache(monkeypatch):
    monkeypatch.setattr(tokens.time, "sleep", lambda _s: None)
    tokens._price_cache.clear()
    yield
    tokens._price_cache.clear()


def test_stablecoin_is_pegged():
    assert tokens.get_quote_token_usd_price(config.USDG) == 1.0


def test_unknown_token_has_no_price():
    assert tokens.get_quote_token_usd_price("0x000000000000000000000000000000000000dEaD") is None


def test_flaky_feed_is_retried_and_cached():
    feed = mock.Mock(side_effect=[TimeoutError("timeout"), 180.0])
    with mock.patch.dict(tokens._EXTERNAL_PRICE_FEEDS, {config.NVDA: feed}):
        assert tokens.get_quote_token_usd_price(config.NVDA) == 180.0
        assert tokens.get_quote_token_usd_price(config.NVDA) == 180.0
    assert feed.call_count == 2


def test_failure_is_cached_so_a_run_stays_consistent():
    feed = mock.Mock(side_effect=TimeoutError("down"))
    with mock.patch.dict(tokens._EXTERNAL_PRICE_FEEDS, {config.NVDA: feed}):
        assert tokens.get_quote_token_usd_price(config.NVDA) is None
        assert tokens.get_quote_token_usd_price(config.NVDA) is None
    assert feed.call_count == tokens._PRICE_FETCH_ATTEMPTS


def test_cached_price_expires():
    feed = mock.Mock(side_effect=[100.0, 200.0])
    with mock.patch.dict(tokens._EXTERNAL_PRICE_FEEDS, {config.NVDA: feed}):
        assert tokens.get_quote_token_usd_price(config.NVDA) == 100.0
        tokens._price_cache[config.NVDA] = (0.0, 100.0)  # pretend it was fetched long ago
        assert tokens.get_quote_token_usd_price(config.NVDA) == 200.0

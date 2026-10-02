from unittest import mock

from liquidity_depth import config, price_check

TOKEN = "0x2e8C31162B855a2fFa90F6F8634643aD6F111E18"
BASE = {"address": TOKEN.lower()}


def _response(pairs):
    resp = mock.Mock()
    resp.json.return_value = {"pairs": pairs}
    return resp


def test_uses_highest_liquidity_pair_on_this_chain():
    pairs = [
        {"chainId": config.DEXSCREENER_CHAIN_ID, "baseToken": BASE, "priceUsd": "0.10", "liquidity": {"usd": 50}},
        {"chainId": config.DEXSCREENER_CHAIN_ID, "baseToken": BASE, "priceUsd": "0.15", "liquidity": {"usd": 900_000}},
        {"chainId": "ethereum", "baseToken": BASE, "priceUsd": "9.99", "liquidity": {"usd": 10**9}},
    ]
    with mock.patch.object(price_check.requests, "get", return_value=_response(pairs)):
        result = price_check.check_price(TOKEN, 0.15)
    assert result.available and result.external_price_usd == 0.15
    assert not result.diverged


def test_ignores_pairs_where_token_is_only_the_quote():
    # priceUsd is the *base* token's price - a deeper pair quoted in our
    # token must not be read as our token's price.
    pairs = [
        {"chainId": config.DEXSCREENER_CHAIN_ID, "baseToken": {"address": "0x" + "11" * 20}, "priceUsd": "29.83", "liquidity": {"usd": 2_000_000}},
        {"chainId": config.DEXSCREENER_CHAIN_ID, "baseToken": BASE, "priceUsd": "0.045", "liquidity": {"usd": 400_000}},
    ]
    with mock.patch.object(price_check.requests, "get", return_value=_response(pairs)):
        result = price_check.check_price(TOKEN, 0.045)
    assert result.external_price_usd == 0.045
    assert not result.diverged


def test_flags_divergence_above_threshold():
    pairs = [{"chainId": config.DEXSCREENER_CHAIN_ID, "baseToken": BASE, "priceUsd": "1.0", "liquidity": {"usd": 1}}]
    computed = 1.0 + config.PRICE_CHECK_THRESHOLD_PCT / 100 + 0.05
    with mock.patch.object(price_check.requests, "get", return_value=_response(pairs)):
        assert price_check.check_price(TOKEN, computed).diverged


def test_network_error_means_unavailable_not_diverged():
    with mock.patch.object(price_check.requests, "get", side_effect=ConnectionError("offline")):
        result = price_check.check_price(TOKEN, 0.15)
    assert not result.available and not result.diverged

from types import SimpleNamespace

from liquidity_depth import pools_v4

TOKEN = "0x98096d17e191B3dA1d5f99a6D7b3584351b11E18"


def test_initialize_filters_match_token_as_currency0_and_currency1(monkeypatch):
    # Initialize(PoolId indexed id, Currency indexed currency0, Currency
    # indexed currency1, ...): topics are [signature, id, currency0,
    # currency1]. A filter one position off silently finds nothing for
    # every pool where the token is currency1.
    seen = []
    monkeypatch.setattr(pools_v4, "get_logs_with_fallback", lambda w3, params, a, b: seen.append(params["topics"]) or [])
    w3 = SimpleNamespace(eth=SimpleNamespace(block_number=100))

    assert pools_v4.discover_v4_pools(w3, TOKEN, use_cache=False) == []

    token_topic = pools_v4._address_topic(TOKEN)
    init_topic = pools_v4._init_event_topic()
    assert sorted(seen, key=len) == [
        [init_topic, None, token_topic],
        [init_topic, None, None, token_topic],
    ]

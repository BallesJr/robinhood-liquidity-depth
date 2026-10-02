from eth_abi import decode as abi_decode
from eth_abi import encode as abi_encode

from liquidity_depth import config, rpc_utils

POOL = "0x1111111111111111111111111111111111111111"
REVERTS = "0x2222222222222222222222222222222222222222"


def _plain_result(call: dict) -> bytes:
    """What the fake node returns for one plain eth_call: a word derived
    from the calldata, or b"" if the target reverts."""
    if call["to"] == REVERTS:
        return b""
    return int(call["data"][2:], 16).to_bytes(32, "big")


class FakeNode:
    """Stands in for rpc_utils._plain_batch_eth_call: answers plain calls
    and Multicall3 aggregate3() calls, and records how many eth_calls it
    was sent."""

    def __init__(self, multicall_works: bool = True):
        self.multicall_works = multicall_works
        self.eth_calls = 0

    def __call__(self, w3, calls, batch_size, progress_label):
        self.eth_calls += len(calls)
        return [self._answer(c) for c in calls]

    def _answer(self, call: dict) -> bytes:
        if call["to"] != config.MULTICALL3_ADDRESS:
            return _plain_result(call)
        if not self.multicall_works:
            return b""
        (inner,) = abi_decode(["(address,bool,bytes)[]"], bytes.fromhex(call["data"][10:]))
        results = []
        for target, _allow_failure, data in inner:
            r = _plain_result({"to": target, "data": "0x" + data.hex()})
            results.append((bool(r), r))
        return abi_encode(["(bool,bytes)[]"], [results])


def _calls(n: int) -> list[dict]:
    calls = [{"to": POOL, "data": "0x" + format(i + 1, "08x")} for i in range(n)]
    calls[2] = {"to": REVERTS, "data": "0x00000000"}
    return calls


def test_multicall_matches_plain_calls_and_keeps_order(monkeypatch):
    node = FakeNode()
    monkeypatch.setattr(rpc_utils, "_plain_batch_eth_call", node)
    monkeypatch.setattr(config, "MULTICALL_CHUNK_SIZE", 3)
    calls = _calls(7)

    results = rpc_utils.batch_eth_call(None, calls)

    assert results == [_plain_result(c) for c in calls]
    assert results[2] == b""  # a reverting sub-call doesn't sink its group
    assert node.eth_calls == 3  # 7 calls packed into ceil(7/3) aggregates


def test_failed_aggregate_falls_back_to_plain_calls(monkeypatch):
    node = FakeNode(multicall_works=False)
    monkeypatch.setattr(rpc_utils, "_plain_batch_eth_call", node)
    monkeypatch.setattr(config, "MULTICALL_CHUNK_SIZE", 3)
    calls = _calls(7)

    assert rpc_utils.batch_eth_call(None, calls) == [_plain_result(c) for c in calls]


def test_multicall_can_be_disabled(monkeypatch):
    node = FakeNode()
    monkeypatch.setattr(rpc_utils, "_plain_batch_eth_call", node)
    monkeypatch.setattr(config, "MULTICALL3_ADDRESS", "")
    calls = _calls(5)

    assert rpc_utils.batch_eth_call(None, calls) == [_plain_result(c) for c in calls]
    assert node.eth_calls == 5

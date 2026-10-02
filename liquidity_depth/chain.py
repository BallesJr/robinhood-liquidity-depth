"""Web3 connection to Robinhood Chain."""
from __future__ import annotations

from web3 import Web3

from . import config
from .rpc_utils import call_with_retry


def get_w3() -> Web3:
    """Connect to Robinhood Chain and sanity-check the chain id."""
    w3 = Web3(Web3.HTTPProvider(config.RPC_URL, request_kwargs={"timeout": config.RPC_TIMEOUT_SECONDS}))
    if not w3.is_connected():
        raise ConnectionError(f"Could not connect to Robinhood Chain RPC at {config.RPC_URL}")
    chain_id = call_with_retry(lambda: w3.eth.chain_id)
    if chain_id != config.CHAIN_ID:
        raise ValueError(
            f"Connected to chain id {chain_id}, expected {config.CHAIN_ID} (Robinhood Chain). "
            "Check RPC_URL in your .env file."
        )
    return w3

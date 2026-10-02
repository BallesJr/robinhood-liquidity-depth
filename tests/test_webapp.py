import json
import time
from unittest import mock

import pytest

from liquidity_depth.models import AnalysisResult, BinRow, PoolInfo, PoolResult, TokenMeta
from webapp import app as webapp

TOKEN = "0x2e8C31162B855a2fFa90F6F8634643aD6F111E18"
QUERY = {"token": TOKEN, "window": "50", "bins": "200"}


def _fake_result():
    target = TokenMeta(TOKEN, "AI", "Artificial Inu", 18)
    quote = TokenMeta("0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168", "USDG", "Global Dollar", 6)
    pool = PoolInfo("v3", "0x" + "ab" * 20, 3000, 60, None, target, quote, 2**96, 0, 10**18, tvl_usd=1000.0)
    return AnalysisResult(
        token=target,
        current_price_usd=0.15,
        price_low=0.1,
        price_high=0.2,
        bins=[BinRow(0.1, 0.2, 0.15, {pool.pool_key: 500.0})],
        pool_results=[PoolResult(pool, quote, True, 0.15, 500.0, True)],
        buyside_usd=300.0,
        sellside_usd=200.0,
        imbalance_usd=100.0,
    )


def _fake_analyze(_w3, _token, window_pct, num_bins, progress=None):
    if progress:
        progress(1, "Reading token metadata")
    return _fake_result()


def _wait_for(client, job_id):
    for _ in range(100):
        job = client.get(f"/api/jobs/{job_id}").get_json()
        if job["status"] != "running":
            return job
        time.sleep(0.02)
    raise AssertionError("job never finished")


@pytest.fixture
def client():
    webapp._result_cache.clear()
    webapp._jobs.clear()
    with mock.patch.object(webapp, "_get_w3", return_value=None):
        yield webapp.app.test_client()


def test_index_renders_empty_state(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"Analyze" in resp.data


@pytest.mark.parametrize(
    "params, message",
    [
        ({"token": "0x123"}, "token address"),
        ({"token": TOKEN, "window": "0"}, "Window"),
        ({"token": TOKEN, "window": "abc"}, "Window"),
        ({"token": TOKEN, "bins": "5"}, "Bins"),
    ],
)
def test_api_rejects_bad_input(client, params, message):
    resp = client.post("/api/analyze", data=params)
    assert resp.status_code == 400
    assert message in resp.get_json()["error"]


def test_job_runs_then_result_is_served_from_cache(client):
    with mock.patch.object(webapp, "analyze_token", side_effect=_fake_analyze) as analyze:
        start = client.post("/api/analyze", data=QUERY).get_json()
        assert start["status"] == "running"
        assert _wait_for(client, start["job_id"])["status"] == "done"

        page = client.get(start["url"])
        assert page.status_code == 200
        assert b"$300" in page.data and b"$200" in page.data

        # The same query again is answered from the cache, not re-analyzed.
        assert client.post("/api/analyze", data=QUERY).get_json()["status"] == "done"
        assert analyze.call_count == 1


def test_refresh_bypasses_cache(client):
    with mock.patch.object(webapp, "analyze_token", side_effect=_fake_analyze) as analyze:
        _wait_for(client, client.post("/api/analyze", data=QUERY).get_json()["job_id"])
        start = client.post("/api/analyze?refresh=1", data=QUERY).get_json()
        _wait_for(client, start["job_id"])
        assert analyze.call_count == 2


def test_job_error_is_reported(client):
    boom = mock.Mock(side_effect=ValueError("No Uniswap V3/V4 pools found for token X"))
    with mock.patch.object(webapp, "analyze_token", boom):
        job = _wait_for(client, client.post("/api/analyze", data=QUERY).get_json()["job_id"])
    assert job["status"] == "error"
    assert "No Uniswap V3 or V4 pools" in job["error"]


def test_unknown_job_is_404(client):
    assert client.get("/api/jobs/nope").status_code == 404


def test_unpriceable_pools_are_folded_into_one_row(client):
    result = _fake_result()
    target = result.token
    for i, symbol in enumerate(("FIRM", "HEDGE")):
        other = TokenMeta("0x" + f"{i + 1:02x}" * 20, symbol, symbol, 18)
        pool = PoolInfo("v4", "0x" + f"{i + 1:02x}" * 32, 3000, 60, None, target, other, 2**96, 0, 10**18)
        result.pool_results.append(PoolResult(pool, other, True, None, 0.0, False, f"no USD price for quote token {symbol}"))

    with mock.patch.object(webapp, "analyze_token", return_value=result):
        html = client.get("/", query_string=QUERY).get_data(as_text=True)

    assert "2 pools paired with tokens that have no USD price" in html
    assert "(FIRM, HEDGE)" in html
    # The summary row comes after the priced pool and before the folded rows.
    assert html.index("USDG</td>") < html.index('<tr class="unpriced-summary">') < html.index("AI/FIRM")
    assert html.count('class="unpriced"') == 2


def test_empty_bins_field_means_auto(client):
    with mock.patch.object(webapp, "analyze_token", side_effect=_fake_analyze) as analyze:
        start = client.post("/api/analyze", data={"token": TOKEN, "window": "50", "bins": ""}).get_json()
        _wait_for(client, start["job_id"])
    assert analyze.call_args.kwargs["num_bins"] is None
    assert "bins" not in start["url"]


def test_result_page_keeps_the_progress_step_labels(client):
    # Starting a new analysis from a result page shows these labels; they
    # once got overwritten by the chart's liquidity levels ("[object Object]").
    with mock.patch.object(webapp, "analyze_token", return_value=_fake_result()):
        html = client.get("/", query_string=QUERY).get_data(as_text=True)
    assert f"const STEPS = {json.dumps(list(webapp.ANALYSIS_STEPS))};" in html

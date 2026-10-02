"""Web UI for the liquidity depth tool.

Run with:
    python -m webapp.app

Then open http://127.0.0.1:5000/.

An analysis takes 10-20 seconds (see docs/METHODOLOGY.md, "Performance
notes"), so the page doesn't block on it: submitting the form starts a
background job (POST /api/analyze), the page polls its progress
(GET /api/jobs/<id>), and once it's done it loads the result page, which is
served from the in-memory result cache. A plain GET /?token=... still works
without JavaScript - it just runs the analysis synchronously.

Results are cached per (token, window, bins) for
config.WEB_RESULT_CACHE_TTL_SECONDS; the "Refresh" button always bypasses
the cache. Jobs and the cache live in process memory, so the production
server must run a single worker process (with threads) - see the Procfile.
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urlencode

from flask import Flask, jsonify, render_template, request

from liquidity_depth import config
from liquidity_depth.aggregation import ANALYSIS_STEPS, analyze_token
from liquidity_depth.chain import get_w3
from liquidity_depth.visualize import build_plotly_figure, liquidity_steps

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")

app = Flask(__name__)

_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
# Pools with less than this much in-window liquidity are collapsed under
# "show all pools" in the table - usually dust/probe pools.
_DUST_THRESHOLD_USD = 1.0

EXAMPLE_TOKENS = [
    {"symbol": "AI", "name": "Artificial Inu", "address": config.DEFAULT_TOKEN},
]

_w3 = None
_w3_lock = threading.Lock()

# (token, window_pct, bins) -> (analyzed_at, AnalysisResult). Separate from
# liquidity_depth.pool_cache (V4 pool *candidates* on disk): this one is
# short-lived and holds complete results, including live prices.
_result_cache: dict[tuple[str, float, int], tuple[float, object]] = {}
_cache_lock = threading.Lock()


@dataclass
class Job:
    key: tuple[str, float, int]
    started_at: float = field(default_factory=time.time)
    step: int = 0
    label: str = "Starting"
    status: str = "running"  # running | done | error
    error: str | None = None
    finished_at: float | None = None


_jobs: dict[str, Job] = {}
_jobs_lock = threading.Lock()


def _get_w3():
    global _w3
    with _w3_lock:
        if _w3 is None:
            _w3 = get_w3()
        return _w3


def _parse_params(args) -> tuple[str, float, int | None, str | None]:
    """Returns (token, window_pct, bins, error). window_pct is in percent;
    bins is None (empty field) to let the analysis pick it per token."""
    token = (args.get("token") or "").strip() or config.DEFAULT_TOKEN
    try:
        window_pct = float(args.get("window", config.DEFAULT_WINDOW_PCT * 100))
    except (TypeError, ValueError):
        return token, config.DEFAULT_WINDOW_PCT * 100, config.DEFAULT_NUM_BINS, "Window must be a number."
    raw_bins = (args.get("bins") or "").strip()
    try:
        bins = int(raw_bins) if raw_bins else config.DEFAULT_NUM_BINS
    except (TypeError, ValueError):
        return token, window_pct, config.DEFAULT_NUM_BINS, "Bins must be a whole number."

    if not _ADDRESS_RE.match(token):
        return token, window_pct, bins, "That doesn't look like a token address - it should be 0x followed by 40 hex characters."
    if not 0 < window_pct <= 500:
        return token, window_pct, bins, "Window must be between 0% and 500%."
    if bins is not None and not 20 <= bins <= 1000:
        return token, window_pct, bins, "Bins must be between 20 and 1000."
    return token, window_pct, bins, None


def _cache_key(token: str, window_pct: float, bins: int | None) -> tuple[str, float, int | None]:
    return token.lower(), window_pct, bins


def _cached_result(key) -> tuple[float, object] | None:
    with _cache_lock:
        cached = _result_cache.get(key)
    if cached and time.time() - cached[0] < config.WEB_RESULT_CACHE_TTL_SECONDS:
        return cached
    return None


def _run_analysis(token: str, window_pct: float, bins: int | None, progress=None) -> tuple[float, object]:
    result = analyze_token(_get_w3(), token, window_pct=window_pct / 100, num_bins=bins, progress=progress)
    # Stamp *after* the fetch completes - analyze_token can take a while, and
    # stamping at the start would make the entry look stale on arrival.
    analyzed_at = time.time()
    with _cache_lock:
        _result_cache[_cache_key(token, window_pct, bins)] = (analyzed_at, result)
        stale_cutoff = analyzed_at - config.WEB_RESULT_CACHE_TTL_SECONDS * 10
        for k, (cached_at, _) in list(_result_cache.items()):
            if cached_at < stale_cutoff:
                del _result_cache[k]
    return analyzed_at, result


def _friendly_error(exc: Exception) -> str:
    message = str(exc)
    if "No Uniswap V3/V4 pools found" in message:
        return "No Uniswap V3 or V4 pools were found for this token against WETH, USDG or NVDA."
    return message


def _job_worker(job_id: str, token: str, window_pct: float, bins: int | None) -> None:
    job = _jobs[job_id]

    def progress(step: int, label: str) -> None:
        job.step, job.label = step, label

    try:
        _run_analysis(token, window_pct, bins, progress=progress)
        job.status = "done"
    except Exception as exc:  # noqa: BLE001 - reported to the page instead of crashing the thread
        logging.getLogger("liquidity_depth").exception("Analysis failed for %s", token)
        job.status, job.error = "error", _friendly_error(exc)
    job.finished_at = time.time()


def _prune_jobs() -> None:
    cutoff = time.time() - 600
    for job_id, job in list(_jobs.items()):
        if job.finished_at and job.finished_at < cutoff:
            del _jobs[job_id]


def _result_url(token: str, window_pct: float, bins: int | None) -> str:
    params = {"token": token, "window": f"{window_pct:g}"}
    if bins is not None:
        params["bins"] = bins
    return "/?" + urlencode(params)


@app.post("/api/analyze")
def api_analyze():
    token, window_pct, bins, error = _parse_params(request.form or request.args)
    if error:
        return jsonify({"error": error}), 400

    url = _result_url(token, window_pct, bins)
    key = _cache_key(token, window_pct, bins)
    if request.args.get("refresh") != "1" and _cached_result(key):
        return jsonify({"status": "done", "url": url})

    with _jobs_lock:
        _prune_jobs()
        # Reuse an in-flight job for the same query instead of starting a second one.
        job_id = next((jid for jid, job in _jobs.items() if job.key == key and job.status == "running"), None)
        if job_id is None:
            job_id = uuid.uuid4().hex
            _jobs[job_id] = Job(key=key)
            threading.Thread(target=_job_worker, args=(job_id, token, window_pct, bins), daemon=True).start()
    return jsonify({"status": "running", "job_id": job_id, "url": url, "total_steps": len(ANALYSIS_STEPS)})


@app.get("/api/jobs/<job_id>")
def api_job(job_id: str):
    job = _jobs.get(job_id)
    if job is None:
        return jsonify({"error": "Unknown job - the server may have restarted. Please try again."}), 404
    return jsonify({
        "status": job.status,
        "step": job.step,
        "total_steps": len(ANALYSIS_STEPS),
        "label": job.label,
        "elapsed": round(time.time() - job.started_at, 1),
        "error": job.error,
    })


def _pool_rows(result) -> list[dict]:
    rows = []
    # Priceable pools first: pools quoted in a token with no USD price are
    # folded into one summary row at the end of the table (see index.html).
    for r in sorted(result.pool_results, key=lambda r: (r.priceable, r.liquidity_in_window_usd), reverse=True):
        pool = r.pool
        key = pool.pool_key
        rows.append({
            "pair": f"{pool.token0.symbol}/{pool.token1.symbol}",
            "version": pool.version.upper(),
            "fee": pool.fee_display,
            "key": key,
            "key_short": f"{key[:6]}…{key[-4:]}",
            # V3 pools are contracts with their own explorer page; a V4
            # poolId is just a hash inside the PoolManager, so it has none.
            "explorer_url": f"{config.BLOCKSCOUT_URL}/address/{key}" if pool.version == "v3" else None,
            "tvl": pool.tvl_usd,
            "liquidity": r.liquidity_in_window_usd,
            "priceable": r.priceable,
            "skip_reason": r.skip_reason,
            "quote_symbol": r.quote_token.symbol,
            "dust": r.priceable and r.liquidity_in_window_usd < _DUST_THRESHOLD_USD,
        })
    return rows


@app.get("/")
def index():
    ran = "token" in request.args
    token, window_pct, bins, error = _parse_params(request.args)
    if not ran:
        token, error = "", None

    context = {
        "token": token,
        "window": f"{window_pct:g}",
        "bins": bins,
        "error": error,
        "result": None,
        "examples": EXAMPLE_TOKENS,
        "steps": list(ANALYSIS_STEPS),
        "price_check_threshold": config.PRICE_CHECK_THRESHOLD_PCT,
        "explorer_url": config.BLOCKSCOUT_URL,
    }
    if not ran or error:
        return render_template("index.html", **context)

    key = _cache_key(token, window_pct, bins)
    cached = None if request.args.get("refresh") == "1" else _cached_result(key)
    if cached:
        analyzed_at, result = cached
    else:
        try:
            analyzed_at, result = _run_analysis(token, window_pct, bins)
        except Exception as exc:  # noqa: BLE001 - show any failure on the page instead of a 500
            context["error"] = _friendly_error(exc)
            return render_template("index.html", **context)

    fig = build_plotly_figure(result)
    context.update({
        "result": result,
        "pool_rows": _pool_rows(result),
        "levels": liquidity_steps(result),  # not "steps": that name is the progress list
        "chart_html": fig.to_html(full_html=False, include_plotlyjs="cdn", config={"displaylogo": False, "responsive": True}),
        "last_updated": datetime.fromtimestamp(analyzed_at).strftime("%H:%M:%S"),
        "cache_age_seconds": int(time.time() - analyzed_at),
    })
    return render_template("index.html", **context)


def main():
    # use_reloader=False is deliberate: the reloader restarts the process on
    # every file change, which wipes the in-memory jobs/result cache. Set
    # FLASK_DEBUG=1 for interactive tracebacks; after editing code, restart
    # `python -m webapp.app` by hand.
    debug = os.getenv("FLASK_DEBUG", "0") == "1"
    app.run(debug=debug, port=int(os.getenv("PORT", "5000")), use_reloader=False, threaded=True)


if __name__ == "__main__":
    main()

# Deploying the web UI

The web UI (`webapp/app.py`) can be hosted at a public URL so anyone can use
it without installing Python. The repo already includes:

- `Procfile`: `gunicorn -w 1 --threads 8 --timeout 300 --bind 0.0.0.0:$PORT webapp.app:app`.
  Analyses run as background jobs tracked in memory, so keep a single
  worker process (`-w 1`) and scale with threads instead.
- `render.yaml`: a Render Blueprint.

No environment variables are required. Every setting in
`liquidity_depth/config.py` has a working default and can be overridden from
the host's dashboard.

## Render

1. Fork or push this repo to your GitHub account.
2. On [render.com](https://render.com), choose **New + → Blueprint** and
   select the repo. Render picks up `render.yaml` automatically.
3. Deploy. You'll get a URL like `https://liquidity-depth.onrender.com`.

Free-tier caveats:

- The service sleeps when idle, so the first request after a while is slow.
- The platform proxy may time out requests before gunicorn does. If
  analyses of tokens with many V4 pools time out, lower `MAX_V4_POOLS`
  with an environment variable.
- `.cache/` lives on ephemeral disk and is cleared on every restart. A
  token's first query after a restart is just slower; this doesn't affect
  correctness.

## Railway

1. Fork or push this repo to your GitHub account.
2. On [railway.app](https://railway.app), choose **New Project → Deploy from
   GitHub repo**. Railway detects the `Procfile` automatically.
3. Generate a public domain from the service's **Settings** tab.

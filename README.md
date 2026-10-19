# Kalshi + Kraken live baseline

A small public-data monitor: one Kraken BTC/USD ticker stream and at most three open Kalshi Bitcoin 15-minute contracts. This first version collects and displays data; it does not place orders.

## Run

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/). From the existing checkout:

```bash
cd /workspace/Kalshi-Bot-V3
UV_CACHE_DIR=/workspace/.cache/uv uv sync --frozen
.venv/bin/python -m feedboard
```

The dashboard listens on port **8000**, bound to loopback by default. For a host that provides its own protected port forwarding, use `--host 0.0.0.0`. There is no dashboard authentication.

No exchange credentials are needed. Allow HTTPS access to `api.elections.kalshi.com`, `api.kraken.com`, and `ws.kraken.com` (including WebSocket upgrades). The HTTP client respects the environment's proxy configuration and verifies TLS certificates.

Options:

```bash
.venv/bin/python -m feedboard --series KXBTC15M --symbol BTC/USD --market-limit 3 --poll-seconds 1
.venv/bin/python -m feedboard --check --timeout 25
```

`KALSHI_SERIES` and `KRAKEN_SYMBOL` provide optional defaults. The live check exits successfully only after both feeds have fresh data and Kalshi has an open market. An empty series, a network denial, or a failed feed returns a nonzero exit code. The default Kalshi series was confirmed against the live public API during setup.

## Behavior and limits

- Kraken uses public WebSocket v2 `ticker` with `event_trigger: bbo`, so bid/ask changes arrive without waiting for a trade. Failed connections retry with capped jittered backoff and take one REST sample per retry when possible. The dashboard shows the actual transport.
- Kalshi polls a maximum of three markets once per second with connection reuse. Limits are configurable from one to five markets. Current dollar-string quote fields take precedence over legacy cent fields; missing prices remain missing.
- The feeds run independently. Timeouts, bounded payloads, rate-limit handling, and reconnects prevent a stalled request from blocking the other feed.
- The service retains only current quotes. Dashboard messages carry the latest state at most ten times per second; slow clients receive no queued history. A maximum of eight streaming clients is supported, with a write timeout for stalled clients.
- The browser changes existing DOM nodes, coalesces paints with `requestAnimationFrame`, and caps its session chart at 120 received observations. The chart is sampled session data, not historical candles or every exchange tick.
- Feed age measures **time since this service received data**. HTTP RTT measures the request duration. Loop delay is the rolling p95 scheduling delay over approximately 30 seconds. These are not exchange-to-screen latency measurements. Kraken may omit source timestamps. Kalshi's metadata `updated_time` is deliberately not treated as a quote timestamp.
- An interrupted feed retains its last values with a retrying/stale status. An empty Kalshi result clears old contracts. Prices are never filled with demo data.

Kalshi remains a REST feed at a one-second cadence. Authenticated Kalshi streaming and true end-to-end latency measurement can be added once this baseline is verified against live markets.

## Validation

```bash
.venv/bin/python -m unittest discover -s tests -v
node --check static/app.js
.venv/bin/python -m feedboard --check --timeout 25
```

The offline tests use explicitly local fake upstreams to exercise parsing, failures, concurrent collection, readiness, and streaming behavior. They do not establish access to the real exchanges. The live check is separate.

`GET /healthz` reports application liveness. `GET /readyz` returns **503** until both feeds have fresh data and at least one Kalshi market. `GET /api/snapshot` returns current values and feed diagnostics; `/api/events` streams named `snapshot` SSE events. These routes do not cause extra exchange requests.

## Development

Use the existing checkout; cloud tasks already provide an isolated environment. Dependency versions are frozen in `uv.lock`. Stop the foreground process with Ctrl+C; the service cancels feed tasks and closes its connections. Additional trading or analysis features can build on the normalized snapshot without adding work to the collection loop.

# Kalshi + Kraken paper trader

A small public-data dashboard with automatic **paper trades** on Kalshi Bitcoin 15-minute Up/Down contracts. Kraken supplies Bitcoin momentum and public trade flow. All orders, cash, fills, and P&L are simulated; there is no live-order API or trading credential path.

## Run

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/). Open a terminal in the downloaded or cloned project folder. These commands work in Windows PowerShell, macOS, and Linux:

```bash
uv sync --frozen
uv run python -m feedboard
```

The dashboard listens on port **8000**, bound to loopback by default. Open that port in your browser on the same computer. For Codespaces or a host with protected port forwarding, use `uv run python -m feedboard --host 0.0.0.0`. There is no dashboard authentication. Stop the app with **Ctrl+C**.

No exchange credentials are needed. Allow HTTPS access to `api.elections.kalshi.com`, `api.kraken.com`, and `ws.kraken.com` (including WebSocket upgrades). The HTTP client respects the environment's proxy configuration and verifies TLS certificates.

Options:

```bash
uv run python -m feedboard --paper-cash 1000 --paper-contracts 5
uv run python -m feedboard --no-paper
uv run python -m feedboard --check --timeout 25
```

Paper trading starts automatically with **$1,000 simulated cash**, **five contracts per entry**, and **one position at a time**. It needs at least 20 seconds of continuous, fresh observations and five distinct feed updates before considering an entry. Interrupted feeds restart warmup; quiet prices naturally fail the momentum thresholds. It waits when conditions do not align.

`KALSHI_SERIES` and `KRAKEN_SYMBOL` provide optional feed defaults. Paper direction mapping supports **KXBTC15M + BTC/USD** only; use `--no-paper` for other feeds. The `--check` command tests the two quote feeds without starting or changing a paper account; it does not validate trade-flow, depth, or paper execution by itself.

## Change trading conditions

Open **Trade settings** beneath the paper strip. Set the boxes, then click **Save settings**. The panel stays collapsed when you do not need it; live price updates do not overwrite edits.

| Control | Default | Effect |
| --- | --- | --- |
| Minimum / maximum buy | 10–75¢ | Every filled contract must fall within these entry prices, including slippage. |
| Minimum profit-sale | 80¢ | Fixed profit target when dynamic exits are off; otherwise a floor for the dynamic range. |
| Dynamic sell range | 80–95¢ | Momentum, trade flow, and liquidity select a target within your saved bounds. |
| Contracts | 5 | Whole contracts per entry, from 1 to 100. |
| Only buy toward the strike | On | Up only while BTC is below the verified strike and rising; Down only while above and falling. |
| Allow new paper buys | On | Turning this off pauses new entries while existing-position exits continue. |
| Hold to settlement | Off | Keep profit targets active, suppress all four early exits, and otherwise wait for the official result. |
| Stop loss / momentum reversal / maximum hold / before expiry | On | Individually enable or disable each early-exit reason when Hold to settlement is off. |

**Risk & entry filters** contains stop loss, maximum spread, maximum hold, cooldown, minimum five-second momentum in basis points, and minimum aligned trade flow in percent. Volatility may increase the momentum requirement; the twenty-second floor scales with the five-second setting. The **95¢ hard profit-exit trigger remains fixed**. Price ranges must be coherent: minimum buy ≤ maximum buy < minimum sale, and an enabled dynamic range must start at or above minimum sale and end at or below 95¢.

**Minimum sale applies to profit-taking. Enabled stop-loss, reversal, hold-time, and expiry exits can sell below it.** Saving normally applies to the next entry. Open positions retain their entry-time exit, stop, hold, and cooldown rules. Settings changes do not create trades, reset cash, or remove history. A stale settings revision from another browser tab is rejected; reload saved settings explicitly before trying again.

To hold for an 80¢+ profit target or the final result, check **Hold to settlement** and click **Save settings**. This overrides stop-loss, reversal, maximum-hold, and pre-expiry sales, regardless of their greyed-out checkmarks. Your individual checkbox preferences and numeric limits are preserved for when you turn Hold off. Fixed/dynamic profit targets and the hard 95¢ profit trigger remain active. If no executable profit exit occurs, the position stays open until Kalshi provides an official result; a losing contract can settle at **0¢** for a full loss of its purchase cost plus entry fees. Missing data or a disappearing contract is never treated as a loss or a win.

If you already have an open trade, optionally check **Apply these exit controls to the open trade** before saving. This changes only the five exit switches on that exact position. Its profit prices, stop-loss distance, hold duration, size, and original entry record remain unchanged. Without this checkbox, only future entries receive the new switches. The panel shows the **actual open trade's exit policy**, separately from the editable settings. If the selected trade closes or changes before saving, the request is rejected instead of modifying a different trade. Uncheck the selection to save for future entries only, or explicitly select the new trade.

The strike comes from Kalshi's official numeric `floor_strike` for supported binary Up/Down contracts. A missing or unverified strike blocks entries while the filter is on; there is no guessed opening price. The comparison uses Kraken's BTC midpoint as a spot proxy, while Kalshi settles using its BRTI benchmark averages. This directional filter does not estimate the contract's settlement probability. The current strike is shown inside the settings panel when available.

## Paper rules

The UI adds one compact strip showing the holding, target, net P&L, and latest action. New buys flash green with **Up** or **Down**; exits flash green for a nonnegative net result or red for a loss. Historical events do not replay on reload. Reduced-motion preferences disable animation while retaining the outcome color.

**Entries:** five- and twenty-second BTC midpoint momentum must agree and clear a recent-volatility floor (defaults of at least 0.75 and 1 basis point respectively). The twenty-second aggressive buy/sell notional imbalance must agree by the configured threshold (default 12%), with at least three recent trades. Strongly opposing large-print flow blocks entry. A large print means a public Kraken trade of at least **$100,000 notional**; it does not identify a trader or establish insider intent.

New entries require fresh WebSocket quotes, listing data, and executable Kalshi depth (at most three seconds old), at least 90 seconds until close, an allowed spread and entry price (defaults 4¢ and 10–75¢), and enough visible depth for the full order within 1¢ of the best ask and the maximum buy price. Up buys YES; Down buys NO. Asks are derived from the opposing outcome's actual bids. Crossed, missing, insufficient, and stale books do not produce fills. By default, the candidate direction must also trend toward the verified strike.

**Exits:** momentum, aggressive flow, large prints, and visible depth support produce an adaptive target within the saved range (default **80–95¢**). Stronger aligned signals hold for a higher target; weaker aligned signals lower it. With dynamic exits disabled, the saved minimum sale becomes the fixed profit target. An observed held-side **bid at or above 95¢ always triggers a profit exit**, taking priority over other profit targets. Profit fills never assume execution below their trigger price. Insufficient depth produces partial exits, with the remainder waiting for another observed book. Neither a trigger nor a displayed price guarantees a complete fill.

When Hold to settlement is off, enabled risk exits can occur below the profit-sale minimum: a configured adverse move (default 12¢), momentum/flow reversal, maximum hold (default 180 seconds), or 30 seconds remaining before close. They sweep available bids. Disabling a reason suppresses only that reason; another enabled reason may still exit. Closed or unfillable positions remain open pending a verified final Kalshi result; winners are never guessed from Kraken or a disappearing contract. Exits can use fresh Kalshi depth even if Kraken is unavailable. A completed position starts the cooldown saved at entry (default twenty seconds).

**Accounting:** entries pay observed asks and exits receive observed bids, including depth/slippage. Estimated general taker fees use `ceil(0.07 × contracts × price × (1 − price) × 100) / 100` for each filled level; product-specific fee schedules may differ. Open P&L marks the remaining position to the best visible bid minus estimated fees, so it is an estimate rather than a guaranteed liquidation value. The strategy is an uncalibrated heuristic for collecting paper-trade evidence, not a measured probability or demonstrated profitable strategy. Snapshot polling can miss intrasecond price touches, and paper fills do not model exchange queue priority or market impact.

## Journal and restart

Cash, open positions, settings, settings revision, cooldown, event IDs, and all trade events are stored locally in **`data/paper.sqlite3`**, which is excluded from Git. New entry events include the exact rules and settings revision used for later analysis. Exit and settlement events record the actual exit switches and their position revision, including any explicitly applied change, without rewriting the entry event. Fills and settings changes become visible only after their journal transaction commits. Disk work runs outside the feed loop. A failed save pauses decisions; an unreadable or invalid existing account is preserved and pauses paper trading instead of silently resetting it. Use one app instance per journal.

Restarting resumes the same account and saved settings, with fresh momentum warmup. `--paper-cash` and `--paper-contracts` seed a new journal only; saved UI settings take precedence thereafter. Older journals are upgraded without resetting balances or trades; an older open position retains its original exit policy. For a separate account, use `--paper-state data/another-paper.sqlite3`. Preserve the journal when updating the code. A read-only **`GET /api/paper/trades?limit=50`** returns recent journal events (up to 200); the UI keeps only the latest twenty notifications. An open position tracks its own contract through rollover until it exits or receives an official settlement.

**`GET /api/paper/settings`** returns settings, their revision, and availability. **`PUT /api/paper/settings`** accepts `{"settings": {"min_buy_cents": 15}, "revision": 0}`; partial updates are atomic and validated. Obtain the current revision first. Errors return 400 for invalid values, 409 for a stale settings revision, or 503 when the paper engine cannot accept a durable update. Same-origin JSON requests are required for changes. A successful settings save never submits a real order.

The same request can optionally include `"apply_exit_controls_to_position": {"ticker": "KXBTC15M-...", "opened_at": 1800000000.0}` using the exact identity from the current snapshot. A stale or closed position returns 409. An explicit malformed or null identity returns 400. A successful response reports `applied_to_position`; applying position-only changes is durable even when the future-entry settings revision does not change.

## Behavior and limits

- Kraken uses public WebSocket v2 `ticker` with `event_trigger: bbo` plus a public `trade` subscription for paper signals. Twenty-second trade flow uses bounded 250 ms buckets and replay protection; historical subscription snapshots are excluded. Failed connections retry with capped jittered backoff and take one REST sample per retry when possible. The dashboard shows the actual transport; REST fallback pauses new paper entries.
- Kalshi polls up to three markets once per second and one selected/held contract's five-level book independently, with connection reuse. A held contract's detail is checked after close or if the listing stops updating, including official settlement. Limits are configurable from one to five markets. Current dollar-string quote fields take precedence over legacy cent fields; missing prices remain missing.
- The feeds run independently. Timeouts, bounded payloads, rate-limit handling, and reconnects prevent a stalled request from blocking the other feed.
- The service retains current quotes plus bounded signal history. Paper decisions run independently at ten checks per second. Dashboard messages carry the latest state at most ten times per second; slow clients receive no queued quote history. Trade IDs recover recent coalesced entry/exit notifications. A maximum of eight streaming clients is supported, with a write timeout for stalled clients.
- The browser changes existing DOM nodes, coalesces paints with `requestAnimationFrame`, and caps its session chart at 120 received observations. The chart is sampled session data, not historical candles or every exchange tick.
- Feed age measures **time since this service received data**. HTTP RTT measures the request duration. Loop delay is the rolling p95 scheduling delay over approximately 30 seconds. These are not exchange-to-screen latency measurements. Kraken may omit source timestamps. Kalshi's metadata `updated_time` is deliberately not treated as a quote timestamp.
- An interrupted feed retains its last values with a retrying/stale status. An empty Kalshi result clears old contracts. Prices are never filled with demo data.

Kalshi remains a REST feed at a one-second cadence. Authenticated Kalshi streaming and true end-to-end latency measurement can be added once this baseline is verified against live markets.

## Validation

```bash
uv run python -m unittest discover -s tests -v
node --check static/app.js
uv run python -m feedboard --check --timeout 25
```

The offline tests use explicitly local fake upstreams and deterministic synthetic signals to exercise parsing, failures, concurrent collection, entry/exit rules, bid/ask accounting, partial fills, settlement, persistence, readiness, and streaming behavior. They do not establish access to the real exchanges or strategy profitability. The live check is separate.

`GET /healthz` reports application liveness. `GET /readyz` returns **503** until both feeds have fresh data and at least one Kalshi market. `GET /api/snapshot` returns current values and feed diagnostics; `/api/events` streams named `snapshot` SSE events. These routes do not cause extra exchange requests.

## Development

Use the existing checkout; cloud tasks already provide an isolated environment. Dependency versions are frozen in `uv.lock`. Stop the foreground process with Ctrl+C; the service cancels feed tasks and closes its connections. Additional trading or analysis features can build on the normalized snapshot without adding work to the collection loop.

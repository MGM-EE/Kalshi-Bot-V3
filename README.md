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

Paper trading starts automatically with **$1,000 simulated cash**, **five contracts per entry**, and **one position at a time**. New entries default to **minutes 5–13**, **two seconds of setup confirmation**, and **at least 35% price-path efficiency over 60 seconds**. The momentum signal still needs at least twenty seconds and five distinct feed updates; the enabled choppiness filter requires the longer sixty-second history. Interrupted feeds restart warmup; quiet prices naturally fail the momentum thresholds. These are experimental defaults for new paper tests, not settings demonstrated to be optimal.

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
| Limit entry distance from floor | Off; 0.10% above / below | Optional maximum BTC distance above and below the verified floor, with independent limits. |
| Allow new paper buys | On | Turning this off pauses new entries while existing-position exits continue. |
| Limit entry minutes | On; 5–13 | New buys from minute 5 inclusive until minute 13 exclusive, measured from close minus fifteen minutes. |
| Confirm the setup | On; 2 seconds | All entry checks must stay satisfied on the same side, with new BTC and book updates. |
| Avoid choppy entries | On; 35% efficiency | Wait for sixty seconds of fresh BTC history and a sufficiently straight sampled price path. |
| Hold to settlement | Off | Keep profit targets active, suppress all four early exits, and otherwise wait for the official result. |
| Stop loss / momentum reversal / maximum hold / before expiry | On | Individually enable or disable each early-exit reason when Hold to settlement is off. |

**Risk & entry filters** contains stop loss, maximum spread, maximum hold, cooldown, minimum five-second momentum in basis points, and minimum aligned trade flow in percent. Volatility may increase the momentum requirement; the twenty-second floor scales with the five-second setting. The **95¢ hard profit-exit trigger remains fixed**. Price ranges must be coherent: minimum buy ≤ maximum buy < minimum sale, and an enabled dynamic range must start at or above minimum sale and end at or below 95¢.

Open **Entry timing & quality** inside Trade settings to adjust or independently disable the three new entry gates. Start/end minutes can be fractional, with `0 ≤ start < end ≤ 15`. The existing requirement of at least ninety seconds remaining always applies, including when the custom window is off. The default 5–13 window skips the first five and final two minutes. A setup still confirming at the end of the window is discarded.

Confirmation lasts 0.1–30 seconds. Each engine check must continue to pass freshness, momentum, flow, strike, enabled trend-quality, price, spread, full-depth, cash, unused-book, and timing rules. A failed check, new contract/side/reference, settings change, restart, clock discontinuity, or decision-loop pause over one second restarts the timer. Repeated cached quotes alone cannot complete it: at least one new BTC observation and book revision are required after confirmation starts.

Trend efficiency is `abs(current BTC midpoint − midpoint sixty seconds earlier) / sum(abs(sampled price changes))`, expressed as a percentage. It is bounded from 0% (flat/backtracking) to 100% (a straight path), and is not a settlement probability. At most one latest observed midpoint per monotonic-second bucket is kept, with no synthetic gap filling and at most 66 points. Stale or invalid BTC data, REST fallback, clock discontinuities, or quote gaps over three seconds clear that history. Higher thresholds permit fewer setups. New **Trend quality** and **Signal confirmation** checks use the existing condition colors. Numeric gate observations are saved with research samples and committed fill inputs for later analysis; one-second research still cannot replay every 100 ms decision exactly.

Updating an older journal automatically enables these three defaults for future entries, retaining cash, history, saved stop-loss distances, exit toggles, and open-position rules. Already saved choices for these new controls survive later restarts. Confirmation and trend history are ephemeral and warm up again after restarting. The new controls add no exchange requests or synchronous disk work. They do not calibrate the displayed settlement forecast or create a probability-versus-price entry model.

**Minimum sale applies to profit-taking. Enabled stop-loss, reversal, hold-time, and expiry exits can sell below it.** Saving normally applies to the next entry. Open positions retain their entry-time exit, stop, hold, and cooldown rules. Settings changes do not create trades, reset cash, or remove history. A stale settings revision from another browser tab is rejected; reload saved settings explicitly before trying again.

To hold for an 80¢+ profit target or the final result, check **Hold to settlement** and click **Save settings**. This overrides stop-loss, reversal, maximum-hold, and pre-expiry sales, regardless of their greyed-out checkmarks. Your individual checkbox preferences and numeric limits are preserved for when you turn Hold off. Fixed/dynamic profit targets and the hard 95¢ profit trigger remain active. If no executable profit exit occurs, the position stays open until Kalshi provides an official result; a losing contract can settle at **0¢** for a full loss of its purchase cost plus entry fees. Missing data or a disappearing contract is never treated as a loss or a win.

If you already have an open trade, optionally check **Apply these exit controls to the open trade** before saving. This changes only the five exit switches on that exact position. Its profit prices, stop-loss distance, hold duration, size, and original entry record remain unchanged. Without this checkbox, only future entries receive the new switches. The panel shows the **actual open trade's exit policy**, separately from the editable settings. If the selected trade closes or changes before saving, the request is rejected instead of modifying a different trade. Uncheck the selection to save for future entries only, or explicitly select the new trade.

The strike comes from Kalshi's official numeric `floor_strike` for supported binary Up/Down contracts. A missing or unverified strike blocks entries while either strike filter is on; there is no guessed opening price. The comparison uses Kraken's BTC midpoint as a spot proxy, while Kalshi settles using its BRTI benchmark averages. This directional filter does not estimate the contract's settlement probability. The current strike is shown inside the settings panel when available.

To limit entry distance, enable **Limit entry distance from floor**, set **Maximum below %** and **Maximum above %**, and save. Here the floor is the BTC strike price, not the contract's cents per share. `0.10` means **0.10%**. With an $80,000 floor, 0.20% below and 0.10% above allow BTC midpoints from **$79,840 through $80,080**, inclusive. The live preview shows the range for the current floor. This saved filter applies only to new entries, alongside all other enabled entry conditions; it does not change exits or an open trade's rules.

## Settlement outlook

The **Settlement outlook** panel below the Paper strip shows experimental **Up / Down percentages**, time remaining, progress through the 15-minute contract, and the observed Up-estimate history. Down is the complement of Up. The panel follows the same selected or held Kalshi contract as the verified floor; it does not mix a new contract's floor with an older position.

The model compares Kraken's live BTC midpoint with Kalshi's verified floor, using observed volatility, a bounded short-term momentum adjustment, and time remaining. It needs at least **60 seconds of real WebSocket observations** before displaying percentages. It retains uncertainty for differences between Kraken spot and Kalshi's BRTI settlement average. Live estimates are capped at **5–95%**; only an official Kalshi result can show **0% / 100%**. These are uncalibrated model estimates, not measured accuracy or a demonstrated trading edge.

Missing reference data, stale feeds, and REST fallback hide the current percentages. At close, the panel waits for the official result rather than guessing from spot. The history starts when valid estimates become available, leaves gaps for missing observations, and resets for a different contract or revised floor. It is bounded session history held in memory; restarting clears it. This display adds no exchange requests, changes no entry/exit rules, and writes nothing to the paper journal.

## Paper rules

The UI adds one compact strip showing the holding, target, net P&L, and latest action. New buys flash green with **Up** or **Down**; exits flash green for a nonnegative net result or red for a loss. Historical events do not replay on reload. Reduced-motion preferences disable animation while retaining the outcome color.

Directly below it, **Trade conditions** shows the current entry setup or the held position's exit checks. **Red / Not met** means a condition is unmet, **yellow / Building** means it is approaching its threshold or waiting for data, and **green / Met** means that individual condition is satisfied. Disabled filters appear as **Off**. Expand any condition for its observed value and rule. Entry checks describe one candidate direction at a time; exit checks use the open trade's saved rules and any explicitly applied exit switches. Green checks do not guarantee a fill: fresh executable depth, an unused book observation, and all relevant gates are still required. Exit triggers are alternatives, so they do not all need to turn green.

The panel uses saved settings rather than unsaved form edits. Disconnected or old diagnostics become **Waiting** instead of retaining readiness colors. Reading indicators does not change decisions or the journal. The market-time check describes the saved entry window and the **at least 90 seconds remaining** rule; the default first-five-minutes / final-two-minutes exclusion is now enforced by the engine.

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

## Research collection and export

Research recording starts automatically with the normal paper-trading command:

```bash
uv run python -m feedboard
```

Look for **Research: recording** in the bottom status bar. Keep the app running; the browser may be closed. Research goes into **`data/research.sqlite3`**, separately from your account in **`data/paper.sqlite3`**. Both are excluded from Git. Updating the app must preserve your entire **`data` folder**.

Once per second the recorder saves normalized Kraken bid/ask prices and available source timestamps, twenty-second trade-flow aggregates, Kalshi listing quotes/volume/open interest, the selected or held contract's five-level book, verified floor, market timing, forecast percentages, entry/exit diagnostics, saved configuration, position, and data-age/status information. Sampled features include momentum, realized volatility, directional efficiency/choppiness, strike distance, and whether the sample falls in **minutes 5–13**. That fixed timing flag is for analysis; the saved live entry-window settings determine which buys are allowed. `paper.entry_quality` records the engine's numerical efficiency, warmup coverage, and confirmation progress; committed fill contexts also include `entry_quality`. Exact decision inputs are additionally recorded when a paper fill's authoritative journal transaction commits. Sessions identify the source-code hash and forecast parameters.

Observed contracts are tracked even when no trade occurs. After close, an independent low-rate detail check saves **official Kalshi outcomes and settlement metadata**. Unknown or missing results stay unresolved; they are never inferred from Kraken. Pending checks survive restarts and resume when the app runs again. This adds occasional post-close detail requests, without increasing quote/book polling. The recorder cannot reconstruct markets or prices missed while the app was stopped.

This is **one-second sampled research data**, not a lossless exchange-event archive. Only the selected/held contract has full book depth; other listed contracts have listing quotes. A live CF Benchmarks index feed and the final-minute index average are **not collected**. These limits and stale/missing data are explicitly marked. Disk writes and compression run outside the feed loop, with bounded queues; recorder failures never change the paper strategy. Queue drops are counted, sequence gaps remain detectable, and the status bar flags problems. No data is deleted automatically.

To export after a test session:

1. Stop the app with **Ctrl+C** and let it finish flushing.
2. Run:

   ```bash
   uv run python -m feedboard.export_research
   ```

3. Upload **every ZIP part with the new matching filename prefix** from **`data/exports`**. The export includes research, outcomes, session provenance, and authoritative paper-trade/account history as JSONL. Each ZIP is independently readable and stays below approximately 25 MiB. The original databases are preserved. The exporter reads committed WAL transactions rather than copying a potentially incomplete database file.

For a larger collection, `--date YYYY-MM-DD` selects research snapshots for one **UTC date**; related outcome metadata and the full paper history are retained. If you use custom journal paths, supply matching `--research-state` and `--paper-state` to the exporter. `--research-only` explicitly omits the paper journal.

Research snapshots pause at a default **2 GiB** storage budget, or when free disk space falls below 128 MiB. The status bar reports a save error. Export/archive the evidence, then start a fresh file with `--research-state data/research-next.sqlite3` (and use that path when exporting). `--research-max-mib` adjusts the budget; `--no-research` disables collection. `--check` and `--no-paper` do not start the recorder. Existing paper balances, trades, settings, entry/exit rules, and the settlement display model are unchanged.

## Fast historical simulations

Run the simulator from the project folder in PowerShell, macOS, or Linux:

```bash
uv run python -m feedboard.simulate --days 7
```

The first run downloads public Kalshi BTC 15-minute market metadata, official outcomes, and one-minute bid/ask candles. It batches recent-market requests, limits API request rates, and stores compressed candles in **`data/history.sqlite3`**. Interrupted downloads resume from completed markets. Older dates use Kalshi's historical endpoints where needed; an archived catalog scan can take longer. No API key is required.

After the download, comparisons run locally without waiting for market time. **270 configurations across 664 actual markets took about one second** in a development check; your computer and dataset will affect speed. To repeat with no network requests, including after restarting:

```bash
uv run python -m feedboard.simulate --offline
```

With no date arguments, offline mode reuses the latest cached date range. Online mode defaults to the last seven **completed UTC close-dates**, excluding today. Explicit dates use an inclusive start and exclusive end:

```bash
uv run python -m feedboard.simulate --start 2026-08-01 --end 2026-08-08
uv run python -m feedboard.simulate --days 30 --fetch-only
```

The default grid compares minimum/maximum buy prices, fixed sell targets through 95¢, a settlement baseline, spread limits, and entry windows **0–13, 5–13, and 5–10 minutes**. End minutes are excluded, so **5–13 skips the first five and final two minutes**. To narrow the experiments:

```bash
uv run python -m feedboard.simulate --offline --windows "5-13" --buy-min "10,20" --buy-max "35,40" --sell-targets "80,90,95,settlement"
```

Other options include `--side up`, `--side down`, `--contracts 5`, `--slippage-cents 1`, `--stop-loss-cents 12`, and `--cash 1000`. Stops are off by default. `--sell-targets settlement` removes profit targets; it holds for the official result unless an optional stop is enabled. This baseline differs from the live bot's Hold checkbox, which retains profit targets. Run `--help` for all options. A grid is limited to 2,000 configurations.

Each run writes a unique folder and matching **ZIP in `data/simulations`**. Send that ZIP for analysis. It contains:

- **`summary.json`**: exact UTC range, dataset/code hashes, coverage gaps, timing, assumptions, and the training-selected configuration with its later test results.
- **`leaderboard.csv`**: all configurations ordered by training performance, with separate training and later test metrics, estimated fees, win rate, drawdown, and exit counts.
- **`selected_trades.csv`**: the selected configuration's trades, directions, entry minutes, timestamps, prices, fees, exit reasons, and P&L.
- **`grid.json`** and **`READ_ME.txt`**: reproducible configurations and interpretation notes. Reuse an exported grid with `--grid path/to/grid.json`; also retain the same cached dates, cash, train fraction, and trade-count threshold.

Selection uses only the earlier 70% of markets, with at least ten training trades required by default. A boundary gap prevents unresolved training settlements from leaking into the later period. Training and test each start with the configured cash. The later results never choose the winner. A winning training configuration can lose on the later period, as it did in our seven-day check. Repeated tuning against the same test period removes its value as unseen evidence.

These are **price/time candle experiments**, not a replay of the live strategy or a calibrated mispricing model. Historical candles lack Kraken's five-second momentum, flow, whales, synchronized book depth, and the live dynamic-exit inputs. The simulator buys observed asks and sells observed bids with configurable slippage and estimated general taker fees. It uses only minute **opening quotes**: highs, lows, closes, future volume, and the eventual winner never select entries or trigger earlier fills. No same-minute exit is allowed. Targets must be met after slippage at a later minute open. Available quantity is assumed, so execution remains uncertain. Open trades settle to the official result, using the recorded settlement timestamp when available and close time otherwise; missing timestamps are counted in the report. Drawdown is based on realized equity, and historical fee-schedule changes are not reconstructed.

Missing or crossed quotes are skipped without interpolation. Incomplete downloads stop the simulation by default; `--allow-partial` explicitly permits missing markets and records them. Calendar coverage and missing candles are reported separately. Use `--refresh` online to recheck a cached catalog and candles. The cache, reports, and existing paper/research databases stay local and are excluded from Git. This command neither changes the live bot's settings nor applies a winning simulation configuration automatically. Continue collecting live research to test signals and execution that candles cannot represent.

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

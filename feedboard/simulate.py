"""Run fast cached Kalshi candle experiments: python -m feedboard.simulate."""
from __future__ import annotations

import argparse
import asyncio
import csv
import itertools
import json
import math
import sqlite3
import time
import uuid
import zipfile
import zlib
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .historical import HistoryCache, HistoryDownloader
from .research import source_fingerprint
from .simulation import SimConfig, compare


ASSUMPTIONS = [
    "Price/time candle experiment, NOT a replay of the live momentum/flow bot or a validated mispricing model.",
    "Only observed one-minute bid/ask OPEN values are used. High/low/close and future volume never trigger fills.",
    "Candle opening bid/ask values are aggregates, not a synchronized historical order-book snapshot.",
    "One entry per market; no same-minute exits. Missing/crossed quotes are skipped without forward filling.",
    "With side=both, choose the cheaper eligible ask, with Up as the tie-break; never choose the eventual winner.",
    "Full configured quantity is assumed available: historical quote depth, queue position, missed fills, and market impact are unknown.",
    "Spread is included by buying asks/selling bids; add configured slippage once per side and estimated general taker fees.",
    "Target sells require bid minus slippage to reach the target at a LATER minute open; intraminute touches are not fills.",
    "Unexited trades pay the official binary result at settlement_ts, or at close if that timestamp is missing.",
    "Hold-to-settlement here means no profit target; it is an experimental baseline distinct from the live Hold checkbox.",
    "Training/holdout each start with the same cash. Settlement-boundary markets are purged; selection uses training P&L only.",
    "Drawdown is realized-equity drawdown, not intratrade mark-to-market drawdown. Historical fee schedule changes are not reconstructed.",
    "Repeatedly tuning against the same holdout invalidates its unseen status. Positive historical P&L does not establish a live edge.",
]


def numbers(text, *, low, high):
    try:
        result = [float(part.strip()) for part in text.split(",")]
    except ValueError as error:
        raise ValueError("Expected comma-separated numeric values") from error
    if not result or any(not math.isfinite(v) or not low <= v <= high for v in result):
        raise ValueError(f"Values must be finite and between {low} and {high}")
    return result


def configurations(args):
    if args.grid:
        payload = json.loads(args.grid.read_text(encoding="utf-8-sig"))
        entries = payload.get("configs") if isinstance(payload, dict) else payload
        if not isinstance(entries, list) or not entries or len(entries) > 2000:
            raise ValueError("Grid must contain 1–2000 configuration objects")
        try:
            return [SimConfig(**entry) for entry in entries]
        except TypeError as error:
            raise ValueError("Grid contains unknown fields or a non-object configuration") from error
    minima = numbers(args.buy_min, low=1, high=95)
    maxima = numbers(args.buy_max, low=1, high=95)
    targets = [None if value.strip().lower() == "settlement" else numbers(value, low=1, high=95)[0]
               for value in args.sell_targets.split(",")]
    spreads = numbers(args.spreads, low=0, high=20)
    windows = []
    for value in args.windows.split(","):
        try:
            start, end = map(int, value.strip().split("-"))
        except ValueError as error:
            raise ValueError("Windows must look like 0-13,5-13,5-10") from error
        if not 0 <= start < end <= 15:
            raise ValueError("Entry windows must stay within minutes 0–15")
        windows.append((start, end))
    grid = []
    for minimum, maximum, target, window, spread in itertools.product(minima, maxima, targets, windows, spreads):
        if minimum > maximum or target is not None and maximum >= target:
            continue
        grid.append(SimConfig(minimum, maximum, target, *window, spread, args.side,
                              args.contracts, args.slippage_cents, args.stop_loss_cents))
        if len(grid) > 2000:
            raise ValueError("Grid exceeds 2000 configurations; use fewer values per range")
    if not grid:
        raise ValueError("No coherent configurations; check buy ranges and sell targets")
    return grid


def date_range(args, cache):
    if args.offline and args.start is None and args.end is None and args.days is None:
        return cache.latest_range()
    end = date.fromisoformat(args.end) if args.end else datetime.now(timezone.utc).date()
    start = date.fromisoformat(args.start) if args.start else end - timedelta(days=args.days if args.days is not None else 7)
    today = datetime.now(timezone.utc).date()
    if not start < end <= today or (end - start).days > 3650:
        raise ValueError("Use a positive range of at most 3650 completed UTC days; --end is exclusive")
    return (int(datetime.combine(start, datetime.min.time(), timezone.utc).timestamp()),
            int(datetime.combine(end, datetime.min.time(), timezone.utc).timestamp()))


def write_reports(output, dataset, configs, rows, selected, trades, split, timing, initial_cash):
    prefix = "sim-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:6]
    directory = Path(output) / prefix
    directory.mkdir(parents=True, exist_ok=False)
    summary = {"schema": 1, "mode": "candle_open_price_time_experiment",
               "created_at": datetime.now(timezone.utc).isoformat(), "source_sha256": source_fingerprint(),
               "dataset": dataset, "split": split, "timing": timing, "initial_cash_per_split": initial_cash,
               "configurations": len(rows), "selected_using_training_only": selected,
               "assumptions": ASSUMPTIONS}
    (directory / "summary.json").write_text(json.dumps(summary, indent=2, allow_nan=False), encoding="utf-8")
    (directory / "grid.json").write_text(json.dumps({"configs": [asdict(c) for c in configs]}, indent=2), encoding="utf-8")
    fields = ["config_id", *rows[0]["config"], *[f"{part}_{key}" for part in ("train", "test") for key in rows[0][part]]]
    with (directory / "leaderboard.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({"config_id": row["config_id"], **row["config"],
                             **{f"{part}_{key}": value for part in ("train", "test") for key, value in row[part].items()}})
    with (directory / "selected_trades.csv").open("w", newline="", encoding="utf-8") as stream:
        fields = list(trades[0]) if trades else ["split", "ticker", "side", "entry_ts", "exit_ts", "pnl"]
        writer = csv.DictWriter(stream, fields)
        writer.writeheader()
        writer.writerows(trades)
    (directory / "READ_ME.txt").write_text(
        "CANDLE EXPERIMENT — NO LIVE ORDERS\n\n" + "\n".join(ASSUMPTIONS) +
        "\n\nleaderboard.csv is ordered by TRAINING results. Use the selected configuration's test results for evaluation.\n"
        "summary.json records exact dataset hash, UTC dates, missing data, split, code hash and timing.\n"
        "grid.json can be reused with --grid. Send this ZIP for analysis.\n", encoding="utf-8")
    archive = directory.with_suffix(".zip")
    with zipfile.ZipFile(archive, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zipped:
        for path in sorted(directory.iterdir()):
            zipped.write(path, path.name)
    return archive, summary


def parser():
    result = argparse.ArgumentParser(description="Fast cached Kalshi BTC price/time candle experiments; never places orders")
    result.add_argument("--days", type=int, help="Completed UTC days to download; default 7 online, latest cached range offline")
    result.add_argument("--start", help="First close-date, YYYY-MM-DD UTC")
    result.add_argument("--end", help="Exclusive UTC close-date, YYYY-MM-DD; default today")
    result.add_argument("--cache", type=Path, default=Path("data/history.sqlite3"))
    result.add_argument("--output-dir", type=Path, default=Path("data/simulations"))
    result.add_argument("--offline", action="store_true", help="Run entirely from cache with zero network calls")
    result.add_argument("--refresh", action="store_true", help="Refresh catalog and candles; retain existing cache if a request fails")
    result.add_argument("--fetch-only", action="store_true", help="Download/cache without running simulations")
    result.add_argument("--allow-partial", action="store_true", help="Explicitly allow markets missing downloads; reports disclose gaps")
    result.add_argument("--grid", type=Path, help="JSON list or {configs:[...]} of SimConfig objects; overrides grid flags")
    result.add_argument("--buy-min", default="10,20,30", help="Comma-separated minimum buy cents")
    result.add_argument("--buy-max", default="35,40,50", help="Comma-separated maximum buy cents, including slippage")
    result.add_argument("--sell-targets", default="65,80,90,95,settlement", help="Target cents or settlement")
    result.add_argument("--windows", default="0-13,5-13,5-10", help="Comma-separated entry windows, end minute excluded")
    result.add_argument("--spreads", default="2,4", help="Maximum bid/ask spread cents")
    result.add_argument("--side", choices=("up", "down", "both"), default="both")
    result.add_argument("--contracts", type=int, default=5)
    result.add_argument("--slippage-cents", type=float, default=1.)
    result.add_argument("--stop-loss-cents", type=float)
    result.add_argument("--cash", type=float, default=1000.)
    result.add_argument("--train-fraction", type=float, default=.7)
    result.add_argument("--min-train-trades", type=int, default=10)
    return result


def main():
    arguments = parser()
    args = arguments.parse_args()
    try:
        if args.offline and args.refresh:
            raise ValueError("--offline cannot be combined with --refresh")
        if args.days is not None and args.days <= 0:
            raise ValueError("--days must be positive")
        if args.offline and not args.cache.is_file():
            raise ValueError("History cache is missing; run online once first")
        if not math.isfinite(args.cash) or args.cash <= 0 or not math.isfinite(args.train_fraction) or not 0 < args.train_fraction < 1 or args.min_train_trades < 1:
            raise ValueError("Check --cash, --train-fraction and --min-train-trades")
        configs = configurations(args)
        cache = HistoryCache(args.cache)
        cache.initialize()
        start, end = date_range(args, cache)
        began = time.perf_counter()
        if not args.offline:
            asyncio.run(HistoryDownloader(cache).download(start, end, refresh=args.refresh, allow_partial=args.allow_partial))
        markets, dataset = cache.load(start, end, allow_partial=args.allow_partial)
        if not any(b.yes_bid is not None and b.yes_ask is not None and b.yes_bid <= b.yes_ask for m in markets for b in m.bars):
            raise ValueError("No usable bid/ask quote history in this dataset")
        dataset.update(expected_calendar_rounds=(end - start) // 900,
                       observed_close_slots=len({market.close for market in markets}),
                       missing_settlement_timestamps=sum(not market.settlement_time_known for market in markets))
        print(f"Loaded {len(markets)} settled markets / {sum(len(m.bars) for m in markets)} minute candles.", flush=True)
        if args.fetch_only:
            print(f"Cached in {time.perf_counter() - began:.2f}s. Run again with --offline for local simulation.")
            return
        download_seconds = time.perf_counter() - began
        print(f"Running {len(configs)} price/time configurations locally...", flush=True)
        simulated = time.perf_counter()
        rows, selected, trades, split = compare(configs, markets, initial_cash=args.cash,
            train_fraction=args.train_fraction, min_train_trades=args.min_train_trades)
        simulation_seconds = time.perf_counter() - simulated
        archive, _ = write_reports(args.output_dir, dataset, configs, rows, selected, trades, split,
            {"load_download_seconds": round(download_seconds, 6), "simulation_seconds": round(simulation_seconds, 6)}, args.cash)
        print(f"Ran {len(rows)} configurations across {len(markets)} markets in {simulation_seconds:.3f}s (simulation only).")
        if selected:
            print(f"Training-selected configuration: {selected['config_id']}")
            print(f"Training: {selected['train']['trades']} trades; net ${selected['train']['net_pnl']:.2f}")
            print(f"Later holdout: {selected['test']['trades']} trades; net ${selected['test']['net_pnl']:.2f}")
        else:
            print("No configuration met the minimum training-trade count; no winner selected.")
        print("Candle experiments assume available quantity; BTC signals and intraminute execution are not replayed.")
        print(f"Report ZIP: {archive}")
    except KeyboardInterrupt:
        arguments.exit(130, "Stopped. Completed cache downloads are preserved; rerun to resume.\n")
    except (ValueError, OSError, sqlite3.Error, zlib.error) as error:
        arguments.exit(1, f"Simulation stopped: {error}\nLive bot and paper journals were not changed.\n")


if __name__ == "__main__":
    main()

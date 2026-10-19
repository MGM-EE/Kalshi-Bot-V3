import argparse
import asyncio
import json
import logging
import os
import time
from pathlib import Path

from aiohttp import web

from .core import Settings, Store, start_feeds, stop_feeds
from .web import create_app
from .paper import PaperConfig


async def check(settings: Settings, timeout: float) -> int:
    store = Store(settings)
    session, tasks = await start_feeds(store)
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not store.ready():
            await asyncio.sleep(0.1)
        print(json.dumps({"ready": store.ready(), **store.snapshot()}, indent=2))
        return 0 if store.ready() else 1
    finally:
        await stop_feeds(session, tasks)


def main():
    parser = argparse.ArgumentParser(description="Small public Kalshi + Kraken data feed")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--series", default=os.getenv("KALSHI_SERIES", "KXBTC15M"))
    parser.add_argument("--symbol", default=os.getenv("KRAKEN_SYMBOL", "BTC/USD"))
    parser.add_argument("--market-limit", type=int, default=3)
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    parser.add_argument("--check", action="store_true", help="Get real samples from both feeds, then exit")
    parser.add_argument("--timeout", type=float, default=25, help="Live check deadline in seconds")
    parser.add_argument("--no-paper", action="store_true", help="Run the data dashboard without paper trading")
    parser.add_argument("--paper-state", type=Path, default=Path("data/paper.sqlite3"), help="Local paper journal path")
    parser.add_argument("--paper-cash", type=float, default=1000, help="Starting simulated cash for a NEW journal")
    parser.add_argument("--paper-contracts", type=int, default=5,
                        help="Contracts for a NEW paper journal; saved UI settings take precedence")
    args = parser.parse_args()
    if not 0 < args.timeout <= 300:
        parser.error("--timeout must be between 0 and 300 seconds")
    try:
        settings = Settings(series=args.series, symbol=args.symbol, market_limit=args.market_limit,
                            poll_seconds=args.poll_seconds, paper_enabled=not args.no_paper and not args.check)
        paper_config = (PaperConfig(initial_cash=args.paper_cash, contracts=args.paper_contracts,
                                    series=args.series, symbol=args.symbol) if settings.paper_enabled else None)
    except ValueError as error:
        parser.error(str(error))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.check:
        raise SystemExit(asyncio.run(check(settings, args.timeout)))
    web.run_app(create_app(settings, paper_path=args.paper_state if settings.paper_enabled else None,
                            paper_config=paper_config), host=args.host, port=args.port, access_log=None,
                shutdown_timeout=3, print=lambda _: logging.info("Dashboard listening on port %s", args.port))


if __name__ == "__main__":
    main()

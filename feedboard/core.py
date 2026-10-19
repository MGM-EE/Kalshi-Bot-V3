from __future__ import annotations

import asyncio
import json
import logging
import math
import random
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlsplit
from urllib.request import getproxies, proxy_bypass
from urllib.parse import quote

import aiohttp

from .marketdata import TradeFlow, normalize_orderbook

LOG = logging.getLogger(__name__)
MAX_PAYLOAD = 256 * 1024


def websocket_proxy(url: str) -> str | None:
    """Use the HTTPS proxy for WSS too, while honoring NO_PROXY for local hosts."""
    parsed = urlsplit(url)
    if proxy_bypass(parsed.hostname or ""):
        return None
    proxies = getproxies()
    return proxies.get(parsed.scheme) or proxies.get("https" if parsed.scheme == "wss" else "http")


def retry_after_seconds(value: str | None) -> float:
    """Respect numeric and HTTP-date cooldowns without shortening a server's limit."""
    if value is None:
        return 5.0
    try:
        seconds = float(value)
        if math.isfinite(seconds):
            return max(0.0, seconds)
    except (TypeError, ValueError, OverflowError):
        pass
    try:
        when = parsedate_to_datetime(value)
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return max(0.0, when.timestamp() - time.time())
    except (TypeError, ValueError, OverflowError):
        return 5.0


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def number(value: Any) -> float | None:
    """Keep missing prices missing; reject nonfinite values and booleans."""
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def kalshi_price(market: dict, name: str) -> float | None:
    # Current fixed-point dollar fields take precedence over legacy cents.
    dollar_key = f"{name}_dollars"
    value = number(market.get(dollar_key))
    if dollar_key not in market:
        cents = number(market.get(name))
        value = cents / 100 if cents is not None else None
    return value if value is not None and 0 <= value <= 1 else None


def normalize_kalshi(payload: Any, limit: int) -> list[dict]:
    if not isinstance(payload, dict) or not isinstance(payload.get("markets"), list):
        raise ValueError("Kalshi response has no markets array")
    result = []
    for market in payload["markets"][:limit]:
        if not isinstance(market, dict) or not isinstance(market.get("ticker"), str):
            raise ValueError("Kalshi response contains an invalid market")
        result.append({
            "ticker": market["ticker"],
            "title": market.get("title") or market["ticker"],
            "yes_bid": kalshi_price(market, "yes_bid"),
            "yes_ask": kalshi_price(market, "yes_ask"),
            "no_bid": kalshi_price(market, "no_bid"),
            "no_ask": kalshi_price(market, "no_ask"),
            "last": kalshi_price(market, "last_price"),
            "close_time": market.get("close_time"),
            "market_status": market.get("status"),
            "settlement_result": market.get("result") if market.get("result") in ("yes", "no") else None,
        })
    return result


def normalize_kraken_ticker(payload: Any, symbol: str) -> dict | None:
    if not isinstance(payload, dict) or payload.get("channel") != "ticker":
        return None
    rows = payload.get("data")
    if not isinstance(rows, list):
        raise ValueError("Kraken ticker has no data array")
    for row in rows:
        if not isinstance(row, dict) or row.get("symbol") != symbol:
            continue
        prices = {key: number(row.get(key)) for key in ("bid", "ask", "last")}
        if any(value is None or value <= 0 for value in prices.values()):
            raise ValueError("Kraken ticker contains invalid prices")
        quantities = {key: number(row.get(key)) for key in ("bid_qty", "ask_qty") if key in row}
        return {"symbol": symbol, **prices, **quantities}
    return None


def normalize_kraken_rest(payload: Any, symbol: str) -> dict:
    if not isinstance(payload, dict) or payload.get("error"):
        raise ValueError("Kraken REST returned an API error")
    result = payload.get("result")
    if not isinstance(result, dict) or len(result) != 1:
        raise ValueError("Kraken REST returned an unexpected pair")
    row = next(iter(result.values()))
    if not isinstance(row, dict) or any(not isinstance(row.get(key), list) or not row[key]
                                        for key in ("b", "a", "c")):
        raise ValueError("Kraken REST contains invalid prices")
    try:
        prices = {key: number(row[field][0]) for key, field in
                  (("bid", "b"), ("ask", "a"), ("last", "c"))}
    except (KeyError, IndexError, TypeError) as error:
        raise ValueError("Kraken REST contains invalid prices") from error
    if any(value is None or value <= 0 for value in prices.values()):
        raise ValueError("Kraken REST contains invalid prices")
    return {"symbol": symbol, **prices}


@dataclass(frozen=True)
class Settings:
    kalshi_url: str = "https://api.elections.kalshi.com/trade-api/v2/markets"
    kraken_rest_url: str = "https://api.kraken.com/0/public/Ticker"
    kraken_ws_url: str = "wss://ws.kraken.com/v2"
    series: str = "KXBTC15M"
    symbol: str = "BTC/USD"
    market_limit: int = 3
    poll_seconds: float = 1.0
    request_timeout: float = 8.0
    stale_seconds: float = 15.0
    retry_base: float = 0.5
    retry_cap: float = 15.0
    paper_enabled: bool = False

    def __post_init__(self):
        if not all(math.isfinite(value) for value in (self.poll_seconds, self.request_timeout,
                                                      self.stale_seconds, self.retry_base, self.retry_cap)):
            raise ValueError("timing values must be finite")
        if not 1 <= self.market_limit <= 5:
            raise ValueError("market_limit must be between 1 and 5")
        if self.poll_seconds < 0.25 or self.request_timeout <= 0:
            raise ValueError("invalid request timing")
        if self.retry_base <= 0 or self.retry_cap < self.retry_base or self.stale_seconds <= 0:
            raise ValueError("invalid retry or freshness timing")


@dataclass
class Feed:
    status: str = "connecting"
    transport: str = "rest"
    error: str | None = None
    received_at: str | None = None
    received_mono: float | None = None
    request_ms: float | None = None
    updates: int = 0
    data: dict = field(default_factory=dict)


class Store:
    """Latest values only: neither feed speed nor slow clients can grow a queue."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.started_at = utc_now()
        self.feeds = {
            "kalshi": Feed(data={"series": settings.series, "markets": []}),
            "kraken": Feed(transport="websocket", data={"symbol": settings.symbol,
                                                        "bid": None, "ask": None, "last": None}),
        }
        self.revision = 0
        self.clients = 0
        self.loop_lags = deque(maxlen=300)
        self.book = {"status": "connecting", "ticker": None, "version": 0,
                     "yes": [], "no": [], "settlement_result": None}
        self.book_mono: float | None = None
        self.paper_engine = None
        self.trade_flow = TradeFlow()
        self.paper = {"enabled": False, "mode": "paper", "status": "paused",
                      "message": "Paper trading is disabled"}

    def book_snapshot(self) -> dict:
        age = (time.monotonic() - self.book_mono) * 1000 if self.book_mono is not None else None
        result = {**self.book, "age_ms": round(age) if age is not None else None}
        if result["status"] == "live" and age is not None and age > 3000:
            result["status"] = "stale"
        return result

    def update(self, name: str, data: dict, transport: str, request_ms: float | None = None):
        feed = self.feeds[name]
        feed.data = data
        feed.transport = transport
        feed.request_ms = round(request_ms, 2) if request_ms is not None else None
        feed.received_at = utc_now()
        feed.received_mono = time.monotonic()
        feed.updates += 1
        feed.status = "live"
        feed.error = None
        self.revision += 1

    def fail(self, name: str, error: Exception):
        feed = self.feeds[name]
        message = f"{type(error).__name__}: {error}"[:240]
        if feed.status != "retrying" or feed.error != message:
            LOG.warning("%s feed: %s", name, message)
        feed.status = "retrying"
        feed.error = message
        self.revision += 1

    def snapshot(self) -> dict:
        now = time.monotonic()
        feeds = {}
        for name, feed in self.feeds.items():
            age = now - feed.received_mono if feed.received_mono is not None else None
            status = feed.status
            if status == "live" and age is not None and age > self.settings.stale_seconds:
                status = "stale"
            data = feed.data
            if name == "kraken" and self.settings.paper_enabled:
                data = {**data, "flow": self.trade_flow.snapshot(now)}
            feeds[name] = {"status": status, "transport": feed.transport, "error": feed.error,
                           "received_at": feed.received_at, "age_ms": round(age * 1000) if age is not None else None,
                           "request_ms": feed.request_ms, "updates": feed.updates, "data": data}
        lags = sorted(self.loop_lags)
        p95 = lags[max(0, math.ceil(len(lags) * 0.95) - 1)] if lags else 0
        return {"started_at": self.started_at, "server_time": utc_now(), "mode": "live",
                "feeds": feeds, "paper": self.paper,
                "metrics": {"clients": self.clients, "loop_lag_ms": round(p95, 2)}}

    def ready(self) -> bool:
        feeds = self.snapshot()["feeds"]
        return all(f["status"] == "live" for f in feeds.values()) and bool(feeds["kalshi"]["data"]["markets"])


class Feeds:
    def __init__(self, store: Store, session: aiohttp.ClientSession):
        self.store = store
        self.settings = store.settings
        self.session = session
        self.flow = store.trade_flow
        self.book_version = time.time_ns() // 1000

    def flow_quote(self, data: dict) -> dict:
        if self.settings.paper_enabled:
            return {**data, "flow": self.flow.snapshot(time.monotonic())}
        return data

    def update_flow(self, payload: dict):
        self.flow.add_payload(payload, self.settings.symbol, time.monotonic(), wall_time=time.time())
        feed = self.store.feeds["kraken"]
        if feed.transport == "websocket" and feed.received_mono is not None:
            feed.data = self.flow_quote(feed.data)
            self.store.revision += 1

    def backoff(self, failures: int) -> float:
        cap = min(self.settings.retry_cap, self.settings.retry_base * 2 ** min(failures - 1, 10))
        return random.uniform(cap * 0.75, cap)

    async def get_json(self, url: str, params: dict) -> tuple[Any, float]:
        started = time.monotonic()
        async with self.session.get(url, params=params) as response:
            response.raise_for_status()
            raw = await response.content.read(MAX_PAYLOAD + 1)
            if len(raw) > MAX_PAYLOAD:
                raise ValueError("Response exceeds the 256 KiB limit")
            # read() can return before EOF; read the rest with the same hard cap.
            while not response.content.at_eof():
                part = await response.content.read(MAX_PAYLOAD + 1 - len(raw))
                raw += part
                if len(raw) > MAX_PAYLOAD:
                    raise ValueError("Response exceeds the 256 KiB limit")
            return json.loads(raw), (time.monotonic() - started) * 1000

    async def kalshi(self):
        failures = 0
        while True:
            started = time.monotonic()
            delay = self.settings.poll_seconds
            failed = False
            try:
                payload, elapsed = await self.get_json(self.settings.kalshi_url, {
                    "series_ticker": self.settings.series, "status": "open", "limit": self.settings.market_limit,
                })
                markets = normalize_kalshi(payload, self.settings.market_limit)
                if not markets:
                    # A successful empty response is not a live market feed. Discard expired markets.
                    self.store.feeds["kalshi"].data = {"series": self.settings.series, "markets": []}
                    raise ValueError(f"No open markets for series {self.settings.series}")
                self.store.update("kalshi", {"series": self.settings.series, "markets": markets}, "rest", elapsed)
                failures = 0
            except (aiohttp.ClientError, TimeoutError, ValueError) as error:
                failed = True
                self.store.fail("kalshi", error)
                failures += 1
                delay = max(self.settings.poll_seconds, self.backoff(failures))
                if isinstance(error, aiohttp.ClientResponseError) and error.status == 429:
                    value = error.headers.get("Retry-After") if error.headers else None
                    delay = max(delay, retry_after_seconds(value))
            await asyncio.sleep(delay if failed else max(0.01, delay - (time.monotonic() - started)))

    async def kraken(self):
        failures = 0
        while True:
            updates_before = self.store.feeds["kraken"].updates
            try:
                timeout = aiohttp.ClientWSTimeout(ws_receive=30, ws_close=3)
                async with self.session.ws_connect(self.settings.kraken_ws_url, heartbeat=15,
                                                   timeout=timeout, max_msg_size=MAX_PAYLOAD,
                                                   proxy=websocket_proxy(self.settings.kraken_ws_url)) as ws:
                    await ws.send_json({"method": "subscribe", "params": {
                        "channel": "ticker", "symbol": [self.settings.symbol], "event_trigger": "bbo",
                        "snapshot": True,
                    }})
                    if self.settings.paper_enabled:
                        await ws.send_json({"method": "subscribe", "params": {
                            "channel": "trade", "symbol": [self.settings.symbol], "snapshot": False,
                        }})
                    # Receive deadlines include heartbeats: a connected but stalled feed must recover.
                    last_quote = time.monotonic()
                    while True:
                        remaining = 30 - (time.monotonic() - last_quote)
                        if remaining <= 0:
                            raise TimeoutError("Kraken ticker stopped updating")
                        message = await asyncio.wait_for(ws.receive(), timeout=remaining)
                        if message.type == aiohttp.WSMsgType.TEXT:
                            payload = json.loads(message.data)
                            if isinstance(payload, dict) and payload.get("success") is False:
                                raise ValueError("Kraken rejected a feed subscription")
                            if self.settings.paper_enabled and isinstance(payload, dict) and payload.get("channel") == "trade":
                                self.update_flow(payload)
                                continue
                            data = normalize_kraken_ticker(payload, self.settings.symbol)
                            if data:
                                self.store.update("kraken", self.flow_quote(data), "websocket")
                                last_quote = time.monotonic()
                        elif message.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSE,
                                              aiohttp.WSMsgType.ERROR):
                            raise ConnectionError("Kraken WebSocket disconnected")
            except (aiohttp.ClientError, TimeoutError, ValueError, ConnectionError) as error:
                self.store.fail("kraken", error)
                if self.store.feeds["kraken"].updates > updates_before:
                    failures = 0
                failures += 1
                delay = max(1.0, self.backoff(failures))
                if isinstance(error, aiohttp.ClientResponseError) and error.status == 429:
                    value = error.headers.get("Retry-After") if error.headers else None
                    delay = max(delay, retry_after_seconds(value))
                # One public REST sample per retry keeps a limited feed available if WS is blocked.
                try:
                    payload, elapsed = await self.get_json(self.settings.kraken_rest_url,
                                                           {"pair": self.settings.symbol})
                    data = normalize_kraken_rest(payload, self.settings.symbol)
                    self.store.update("kraken", self.flow_quote(data), "rest", elapsed)
                except (aiohttp.ClientError, TimeoutError, ValueError) as rest_error:
                    self.store.fail("kraken", rest_error)
                    if isinstance(rest_error, aiohttp.ClientResponseError) and rest_error.status == 429:
                        value = rest_error.headers.get("Retry-After") if rest_error.headers else None
                        delay = max(delay, retry_after_seconds(value))
                await asyncio.sleep(delay)

    async def orderbook(self):
        """One small book, including a held contract after it leaves the open list."""
        failures = 0
        while True:
            started = time.monotonic()
            position = self.store.paper_engine.snapshot().get("position") if self.store.paper_engine else None
            markets = self.store.feeds["kalshi"].data.get("markets", [])
            selected = position or (min(markets, key=lambda item: item.get("close_time") or "9999") if markets else None)
            if not selected or not isinstance(selected.get("ticker"), str):
                await asyncio.sleep(0.25)
                continue
            ticker = selected["ticker"]
            if ticker != self.store.book.get("ticker"):
                self.store.book = {"status": "connecting", "ticker": ticker, "version": 0,
                                   "yes": [], "no": [], "settlement_result": None}
                self.store.book_mono = None
            delay = self.settings.poll_seconds
            try:
                # Preserve a custom API origin for tests and documented runtime overrides.
                url = self.settings.kalshi_url.rstrip("/") + "/" + quote(ticker, safe="")
                market = next((m for m in markets if m["ticker"] == ticker), None)
                listing = self.store.feeds["kalshi"]
                listing_fresh = (listing.status == "live" and listing.received_mono is not None
                                 and time.monotonic() - listing.received_mono <= 3)
                try:
                    scheduled_close = datetime.fromisoformat(selected.get("close_time", "").replace("Z", "+00:00")).timestamp()
                except (ValueError, AttributeError, TypeError):
                    scheduled_close = None
                if market is None or not listing_fresh or (position and scheduled_close is not None and scheduled_close <= time.time()):
                    detail, _ = await self.get_json(url, {})
                    rows = normalize_kalshi({"markets": [detail.get("market")]}, 1)
                    market = rows[0]
                if market.get("settlement_result") in ("yes", "no") and market.get("market_status") in ("settled", "finalized"):
                    depth = {"yes": [], "no": []}
                else:
                    payload, _ = await self.get_json(url + "/orderbook", {"depth": 5})
                    depth = normalize_orderbook(payload)
                self.book_version += 1
                self.store.book = {**depth, "status": "live", "ticker": ticker, "version": self.book_version,
                                   "close_time": market.get("close_time"),
                                   "market_status": market.get("market_status"),
                                   "settlement_result": market.get("settlement_result")
                                   if market.get("market_status") in ("settled", "finalized") else None}
                self.store.book_mono = time.monotonic()
                self.store.revision += 1
                failures = 0
                delay = max(0.05, delay - (time.monotonic() - started))
            except (aiohttp.ClientError, TimeoutError, ValueError, AttributeError) as error:
                if self.store.book.get("status") != "retrying":
                    LOG.warning("Kalshi liquidity: %s", error)
                self.store.book = {**self.store.book, "status": "retrying", "error": str(error)[:160]}
                self.store.revision += 1
                failures += 1
                delay = max(self.settings.poll_seconds, self.backoff(failures))
                if isinstance(error, aiohttp.ClientResponseError) and error.status == 429:
                    delay = max(delay, retry_after_seconds(error.headers.get("Retry-After") if error.headers else None))
            await asyncio.sleep(delay)


async def monitor_loop(store: Store):
    while True:
        started = time.monotonic()
        await asyncio.sleep(0.1)
        store.loop_lags.append(max(0.0, (time.monotonic() - started - 0.1) * 1000))


async def start_feeds(store: Store) -> tuple[aiohttp.ClientSession, list[asyncio.Task]]:
    timeout = aiohttp.ClientTimeout(total=store.settings.request_timeout, connect=4)
    connector = aiohttp.TCPConnector(limit=4, limit_per_host=2, ttl_dns_cache=300)
    session = aiohttp.ClientSession(timeout=timeout, connector=connector, trust_env=True,
                                    headers={"User-Agent": "kalshi-kraken-feed/0.1"})
    feeds = Feeds(store, session)
    tasks = [asyncio.create_task(feeds.kalshi(), name="kalshi"),
             asyncio.create_task(feeds.kraken(), name="kraken"),
             asyncio.create_task(monitor_loop(store), name="loop-monitor")]
    if store.settings.paper_enabled:
        tasks.append(asyncio.create_task(feeds.orderbook(), name="kalshi-book"))
    return session, tasks


async def stop_feeds(session: aiohttp.ClientSession, tasks: list[asyncio.Task]):
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await session.close()

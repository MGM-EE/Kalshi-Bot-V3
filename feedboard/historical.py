"""Public Kalshi history download/cache, independent of the running paper bot."""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import sqlite3
import time
import zlib
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import aiohttp

from .core import number, retry_after_seconds
from .reference import normalize_reference

BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
SERIES = "KXBTC15M"
CACHE_VERSION = 1
MAX_RESPONSE = 32 * 1024 * 1024


def timestamp(value, *, ceiling=False):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        result = parsed.timestamp()
        return (math.ceil(result) if ceiling else int(result)) if parsed.tzinfo is not None and math.isfinite(result) else None
    except (ValueError, OverflowError, OSError):
        return None


@dataclass(frozen=True, slots=True)
class Bar:
    start: int
    yes_bid: float | None
    yes_ask: float | None


@dataclass(frozen=True, slots=True)
class Market:
    ticker: str
    close: int
    settled: int
    result: str
    strike: float
    bars: tuple[Bar, ...]
    settlement_time_known: bool = True


def market_valid(raw, start, end):
    close = timestamp(raw.get("close_time"))
    return (close is not None and start <= close < end
            and raw.get("status") in ("settled", "finalized")
            and raw.get("result") in ("yes", "no")
            and normalize_reference(raw)["strike_price"] is not None)


def bar_price(distribution, historical):
    if not isinstance(distribution, dict):
        return None
    if "open_dollars" in distribution:
        value = number(distribution["open_dollars"])
    else:
        value = number(distribution.get("open"))
        if value is not None and not historical:
            value /= 100
    return value if value is not None and 0 <= value <= 1 else None


def compile_market(raw, candles, *, historical):
    close = timestamp(raw.get("close_time"))
    if close is None or not market_valid(raw, close, close + 1):
        raise ValueError("Market lacks verified BTC Up/Down settlement metadata")
    if not isinstance(candles, list):
        raise ValueError("Candlestick response has no array")
    bars = {}
    for row in candles:
        if not isinstance(row, dict):
            raise ValueError("Malformed candlestick")
        end = number(row.get("end_period_ts"))
        if end is None or not end.is_integer() or not close - 900 < end <= close:
            continue
        end = int(end)
        if (close - end) % 60:
            continue
        bar = Bar(end - 60, bar_price(row.get("yes_bid"), historical),
                  bar_price(row.get("yes_ask"), historical))
        if end in bars and bars[end] != bar:
            raise ValueError("Conflicting duplicate candle timestamps")
        bars[end] = bar
    settled = timestamp(raw.get("settlement_ts"), ceiling=True)
    return Market(raw["ticker"], close, max(close, settled or close), raw["result"],
                  normalize_reference(raw)["strike_price"], tuple(bars[key] for key in sorted(bars)), settled is not None)


class HistoryCache:
    """Tagged separate SQLite cache; never opens a paper/research DB as a cache."""
    def __init__(self, path):
        self.path = Path(path)

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=5)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables and "history_cache_meta" not in tables:
                raise ValueError("Choose a separate history cache; this file is not a simulation cache")
            if "history_cache_meta" in tables:
                if connection.execute("SELECT version FROM history_cache_meta").fetchone() not in (None, (CACHE_VERSION,)):
                    raise ValueError("Unsupported history cache schema")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS history_cache_meta(version INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS history_markets(
                    ticker TEXT PRIMARY KEY, market_json TEXT NOT NULL,
                    candles BLOB, historical INTEGER NOT NULL, fetched_at TEXT);
                CREATE TABLE IF NOT EXISTS history_datasets(
                    dataset_id TEXT PRIMARY KEY, manifest TEXT NOT NULL);
            """)
            if connection.execute("SELECT version FROM history_cache_meta").fetchone() is None:
                connection.execute("INSERT INTO history_cache_meta VALUES(?)", (CACHE_VERSION,))

    @staticmethod
    def key(start, end):
        return f"{SERIES}:{start}:{end}"

    def manifest(self, start, end):
        with self.connect() as connection:
            row = connection.execute("SELECT manifest FROM history_datasets WHERE dataset_id=?",
                                     (self.key(start, end),)).fetchone()
        return json.loads(row[0]) if row else None

    def latest_range(self):
        with self.connect() as connection:
            row = connection.execute("SELECT manifest FROM history_datasets ORDER BY rowid DESC LIMIT 1").fetchone()
        if not row:
            raise ValueError("No cached dataset; run online once first")
        manifest = json.loads(row[0])
        return manifest["start_ts"], manifest["end_ts"]

    def catalog(self, start, end, markets, metadata):
        with self.connect() as connection:
            for raw, historical in markets:
                connection.execute("""INSERT INTO history_markets(ticker,market_json,historical) VALUES(?,?,?)
                    ON CONFLICT(ticker) DO UPDATE SET market_json=excluded.market_json""",
                    (raw["ticker"], json.dumps(raw, sort_keys=True, separators=(",", ":")), int(historical)))
            manifest = {**metadata, "tickers": [raw["ticker"] for raw, _ in markets],
                        "start_ts": start, "end_ts": end, "series": SERIES,
                        "catalog_fetched_at": datetime.now(timezone.utc).isoformat()}
            connection.execute("INSERT OR REPLACE INTO history_datasets VALUES(?,?)",
                               (self.key(start, end), json.dumps(manifest)))

    def missing(self, tickers, refresh=False):
        result = []
        with self.connect() as connection:
            for ticker in tickers:
                row = connection.execute("SELECT market_json,historical,candles FROM history_markets WHERE ticker=?",
                                         (ticker,)).fetchone()
                if row is None:
                    raise ValueError("Incomplete cache catalog; rerun without --offline")
                if refresh or row[2] is None:
                    result.append((json.loads(row[0]), bool(row[1])))
        return result

    def save_candles(self, raw, candles, historical):
        compile_market(raw, candles, historical=historical)
        packed = zlib.compress(json.dumps(candles, separators=(",", ":"), allow_nan=False).encode(), 3)
        with self.connect() as connection:
            connection.execute("UPDATE history_markets SET candles=?,historical=?,fetched_at=? WHERE ticker=?",
                (packed, int(historical), datetime.now(timezone.utc).isoformat(), raw["ticker"]))

    def load(self, start, end, *, allow_partial=False):
        manifest = self.manifest(start, end)
        if manifest is None:
            raise ValueError("No cached dataset for these UTC dates; run online once first")
        markets, missing = [], []
        digest = hashlib.sha256()
        with self.connect() as connection:
            connection.execute("BEGIN")
            for ticker in sorted(manifest["tickers"]):
                row = connection.execute("SELECT market_json,candles,historical FROM history_markets WHERE ticker=?", (ticker,)).fetchone()
                if row is None or row[1] is None:
                    missing.append(ticker)
                    continue
                digest.update(row[0].encode())
                digest.update(bytes([row[2]]))
                digest.update(row[1])
                markets.append(compile_market(json.loads(row[0]), json.loads(zlib.decompress(row[1])), historical=bool(row[2])))
        if missing and not allow_partial:
            raise ValueError(f"{len(missing)} markets still need candles; rerun online, or explicitly use --allow-partial")
        if not markets:
            raise ValueError("No usable settled markets in this date range")
        return sorted(markets, key=lambda m: (m.close, m.ticker)), {
            **manifest, "sha256": digest.hexdigest(), "missing_tickers": missing,
            "loaded_markets": len(markets), "markets_without_candles": sum(not m.bars for m in markets),
            "missing_minutes": sum(15 - len(m.bars) for m in markets),
            "unusable_quote_minutes": sum(b.yes_bid is None or b.yes_ask is None or b.yes_bid > b.yes_ask
                                          for m in markets for b in m.bars),
        }


class DownloadError(ValueError):
    pass


class HistoryDownloader:
    def __init__(self, cache, *, base_url=BASE_URL, progress=print, request_interval=.2):
        self.cache, self.base_url, self.progress = cache, base_url.rstrip("/"), progress
        self.interval = request_interval
        self.lock = asyncio.Lock()
        self.next_request = 0.

    async def get(self, session, path, params=None):
        for attempt in range(4):
            async with self.lock:
                await asyncio.sleep(max(0, self.next_request - time.monotonic()))
                self.next_request = time.monotonic() + self.interval
            try:
                async with session.get(self.base_url + path, params=params) as response:
                    if response.status in (429, 500, 502, 503, 504):
                        delay = retry_after_seconds(response.headers.get("Retry-After")) if response.status == 429 else 2 ** attempt
                        if delay > 30 or attempt == 3:
                            raise DownloadError(f"HTTP {response.status}; retry download later (server delay {delay:g}s)")
                        async with self.lock:
                            self.next_request = max(self.next_request, time.monotonic() + delay)
                        continue
                    if response.status == 404:
                        return None
                    response.raise_for_status()
                    buffer = bytearray()
                    async for chunk in response.content.iter_chunked(65536):
                        buffer.extend(chunk)
                        if len(buffer) > MAX_RESPONSE:
                            raise DownloadError("Historical response exceeds the safety limit")
                    payload = json.loads(buffer)
                    if not isinstance(payload, dict):
                        raise DownloadError("Historical response is not an object")
                    return payload
            except (aiohttp.ClientError, TimeoutError) as error:
                if attempt == 3:
                    raise DownloadError(f"History request failed: {type(error).__name__}") from error
                await asyncio.sleep(2 ** attempt)
        raise DownloadError("History request did not complete")

    async def fetch_catalog(self, session, start, end):
        cutoff_payload = await self.get(session, "/historical/cutoff")
        cutoff = timestamp((cutoff_payload or {}).get("market_settled_ts"))
        if cutoff is None:
            raise DownloadError("Historical cutoff unavailable")
        found, rejected = {}, 0
        # Always query live: a market closing before the cutoff may settle after
        # it. Archive queries cannot currently filter by close timestamp.
        paths = [("/markets", False)]
        if start < cutoff:
            paths.append(("/historical/markets", True))
        for path, historical in paths:
            cursor, cursors = None, set()
            for _ in range(1000):
                params = {"series_ticker": SERIES, "limit": 1000}
                if not historical:
                    params.update(min_close_ts=start, max_close_ts=end - 1)
                if cursor:
                    params["cursor"] = cursor
                payload = await self.get(session, path, params)
                if payload is None or not isinstance(payload.get("markets"), list):
                    raise DownloadError("Market catalog unavailable")
                for raw in payload["markets"]:
                    if not isinstance(raw, dict):
                        raise DownloadError("Invalid market catalog row")
                    close = timestamp(raw.get("close_time"))
                    if close is not None and start <= close < end:
                        if market_valid(raw, start, end):
                            found.setdefault(raw["ticker"], (raw, historical))
                        else:
                            rejected += 1
                cursor = payload.get("cursor")
                if not cursor:
                    break
                if not isinstance(cursor, str) or cursor in cursors:
                    raise DownloadError("Repeated or invalid market pagination cursor")
                cursors.add(cursor)
            else:
                raise DownloadError("Market catalog exceeds pagination safety limit")
        markets = sorted(found.values(), key=lambda item: (timestamp(item[0]["close_time"]), item[0]["ticker"]))
        if not markets:
            raise DownloadError("No verified, officially settled BTC markets in this date range")
        self.cache.catalog(start, end, markets, {"cutoff": cutoff_payload, "rejected_market_rows": rejected})

    async def single(self, session, raw, historical):
        ticker = quote(raw["ticker"], safe="")
        close = timestamp(raw["close_time"])
        params = {"start_ts": close - 900, "end_ts": close, "period_interval": 1}
        empty = None
        for archived in (historical, not historical):
            path = f"/historical/markets/{ticker}/candlesticks" if archived else f"/series/{SERIES}/markets/{ticker}/candlesticks"
            response = await self.get(session, path, params)
            if response is not None and isinstance(response.get("candlesticks"), list):
                if response["candlesticks"]:
                    self.cache.save_candles(raw, response["candlesticks"], archived)
                    return
                empty = archived
        if empty is not None:
            self.cache.save_candles(raw, [], empty)
            return
        raise DownloadError(f"No candle response for {raw['ticker']}")

    async def batch(self, session, group):
        closes = [timestamp(raw["close_time"]) for raw, _ in group]
        payload = await self.get(session, "/markets/candlesticks", {
            "market_tickers": ",".join(raw["ticker"] for raw, _ in group),
            "start_ts": min(closes) - 900, "end_ts": max(closes), "period_interval": 1})
        rows = payload.get("markets", []) if payload else []
        if not isinstance(rows, list):
            raise DownloadError("Invalid batch candles response")
        mapped = {row.get("market_ticker", row.get("ticker")): row for row in rows if isinstance(row, dict)}
        for raw, historical in group:
            row = mapped.get(raw["ticker"])
            if row is None or not row.get("candlesticks"):
                await self.single(session, raw, historical)
            else:
                self.cache.save_candles(raw, row["candlesticks"], False)

    async def download(self, start, end, *, refresh=False, allow_partial=False):
        timeout = aiohttp.ClientTimeout(total=25, connect=8)
        connector = aiohttp.TCPConnector(limit=4, limit_per_host=4)
        async with aiohttp.ClientSession(timeout=timeout, connector=connector, trust_env=True,
                headers={"User-Agent": "kalshi-paper-research/1"}) as session:
            if refresh or self.cache.manifest(start, end) is None:
                self.progress("Downloading settled market catalog (live + archive where needed)...")
                await self.fetch_catalog(session, start, end)
            manifest = self.cache.manifest(start, end)
            pending = self.cache.missing(manifest["tickers"], refresh)
            if not pending:
                self.progress(f"Cache hit: {len(manifest['tickers'])} markets; no network download needed.")
                return
            # A group's maximum possible minute count stays below the API's
            # 10,000-candle ceiling even if contracts traded before their round.
            groups, current = [], []
            for item in pending:
                raw, historical = item
                if historical:
                    groups.append([item])
                    continue
                span = (timestamp(raw["close_time"]) - timestamp(current[0][0]["close_time"]) + 900) / 60 if current else 15
                if current and (len(current) >= 20 or (len(current) + 1) * span > 9000):
                    groups.append(current)
                    current = []
                current.append(item)
            if current:
                groups.append(current)
            jobs = asyncio.Queue()
            for group in groups:
                jobs.put_nowait(group)
            errors, completed = [], 0

            async def worker():
                nonlocal completed
                while not jobs.empty():
                    group = jobs.get_nowait()
                    try:
                        if len(group) == 1:
                            await self.single(session, *group[0])
                        else:
                            await self.batch(session, group)
                    except (ValueError, sqlite3.Error, OSError) as error:
                        errors.append(str(error))
                    completed += len(group)
                    self.progress(f"Fetched/checked {completed}/{len(pending)} uncached markets.")

            await asyncio.gather(*(worker() for _ in range(min(4, len(groups)))))
            if errors:
                self.progress(f"Download warnings ({len(errors)}): " + "; ".join(errors[:3]))
                if not allow_partial:
                    raise DownloadError("Incomplete download; cached successes are preserved. Rerun to resume.")

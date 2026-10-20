"""Sampled research evidence, isolated from the paper account and feed loop.

This records one-second normalized views and actual paper decision steps,
not a lossless exchange tick archive.
Quotes are Kraken spot proxies; a live settlement-index feed is not collected.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import math
import shutil
import sqlite3
import time
import uuid
import zlib
from collections import Counter, deque
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import aiohttp

from .core import normalize_kalshi, retry_after_seconds
from .decision_trace import ANCHOR_SECONDS, FORMAT, DecisionEncoder, decision_frame, finite_copy
from .outlook import INDEX_UNCERTAINTY, MOMENTUM_SECONDS, VOLATILITY_FLOOR
from .paper import _fresh, _num, _timestamp

LOG = logging.getLogger(__name__)
SCHEMA = 1
INTERVAL = 1.0
QUEUE_LIMIT = 120
DEFAULT_MAX_BYTES = 2048 * 1024 * 1024


def iso(stamp=None):
    return datetime.fromtimestamp(time.time() if stamp is None else stamp, timezone.utc).isoformat(timespec="milliseconds")


def clean(value):
    """Keep evidence JSON finite; malformed numeric upstream values stay missing."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {str(key): clean(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, deque)):
        return [clean(item) for item in value]
    return value


def source_fingerprint():
    root = Path(__file__).resolve().parent
    digest = hashlib.sha256()
    for path in sorted(root.glob("*.py")):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


class ResearchDB:
    def __init__(self, path, *, max_bytes=DEFAULT_MAX_BYTES):
        self.path = Path(path)
        self.max_bytes = max_bytes

    def check_space(self):
        size = sum(path.stat().st_size for path in (self.path, Path(str(self.path) + "-wal")) if path.exists())
        if size >= self.max_bytes:
            raise OSError("Research size limit reached; export and use a new --research-state file")
        if shutil.disk_usage(self.path.parent).free < 128 * 1024 * 1024:
            raise OSError("Research paused to preserve free disk space; export/archive data")

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=1)
        try:
            connection.execute("PRAGMA synchronous=FULL")
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self, session_id, metadata):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables and "research_meta" not in tables:
                raise ValueError("This is not a research database; choose a separate research path")
            if "research_meta" in tables:
                row = connection.execute("SELECT version FROM research_meta").fetchone()
                if row not in (None, (SCHEMA,)):
                    raise ValueError("Unsupported research database schema")
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS research_meta(version INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS sessions(
                    session_id TEXT PRIMARY KEY, started_at TEXT NOT NULL,
                    ended_at TEXT, metadata TEXT NOT NULL, summary TEXT);
                CREATE TABLE IF NOT EXISTS records(
                    session_id TEXT NOT NULL, sequence INTEGER NOT NULL,
                    kind TEXT NOT NULL, captured_at TEXT NOT NULL, ticker TEXT,
                    payload BLOB NOT NULL, PRIMARY KEY(session_id,sequence));
                CREATE INDEX IF NOT EXISTS records_time ON records(captured_at);
                CREATE TABLE IF NOT EXISTS markets(
                    ticker TEXT PRIMARY KEY, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
                    close_at REAL, metadata TEXT NOT NULL, result TEXT, result_at TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0,
                    last_error TEXT);
            """)
            if connection.execute("SELECT version FROM research_meta").fetchone() is None:
                connection.execute("INSERT INTO research_meta VALUES(?)", (SCHEMA,))
            metadata = {**metadata, "source_sha256": source_fingerprint()}
            connection.execute("INSERT OR IGNORE INTO sessions(session_id,started_at,metadata) VALUES(?,?,?)",
                               (session_id, metadata["started_at"], json.dumps(metadata, allow_nan=False)))

    @staticmethod
    def _market(connection, market, observed_at):
        ticker = market.get("ticker")
        if not isinstance(ticker, str) or not ticker.startswith("KXBTC15M-"):
            return
        row = connection.execute("SELECT metadata,result FROM markets WHERE ticker=?", (ticker,)).fetchone()
        previous = json.loads(row[0]) if row else {}
        merged = {**previous, **{key: value for key, value in market.items() if value is not None}}
        final = merged.get("market_status") in ("settled", "finalized")
        result = merged.get("settlement_result") if final else None
        if result not in ("yes", "no"):
            result = row[1] if row else None
        connection.execute("""INSERT INTO markets(ticker,first_seen,last_seen,close_at,metadata,result,result_at)
            VALUES(?,?,?,?,?,?,?) ON CONFLICT(ticker) DO UPDATE SET
            last_seen=excluded.last_seen, close_at=COALESCE(excluded.close_at,markets.close_at),
            metadata=excluded.metadata, result=COALESCE(markets.result,excluded.result),
            result_at=COALESCE(markets.result_at,excluded.result_at)""",
            (ticker, observed_at, observed_at, _timestamp(merged.get("close_time")),
             json.dumps(clean(merged), allow_nan=False, separators=(",", ":")), result,
             observed_at if result else None))

    def write(self, records):
        self.check_space()
        written = 0
        with self.connect() as connection:
            for record in records:
                payload = clean(record["payload"])
                packed = zlib.compress(json.dumps(payload, allow_nan=False, separators=(",", ":")).encode(), 3)
                cursor = connection.execute("INSERT OR IGNORE INTO records VALUES(?,?,?,?,?,?)",
                    (record["session_id"], record["sequence"], record["kind"], record["captured_at"],
                     record.get("ticker"), packed))
                written += cursor.rowcount
                if record["kind"] == "snapshot":
                    markets = payload.get("feeds", {}).get("kalshi", {}).get("data", {}).get("markets", [])
                    for market in [*markets, payload.get("book", {})]:
                        self._market(connection, market, record["captured_at"])
        return written

    def due_market(self, wall_time):
        with self.connect() as connection:
            row = connection.execute("""SELECT ticker,attempts FROM markets
                WHERE result IS NULL AND close_at <= ? AND next_attempt <= ?
                ORDER BY next_attempt,close_at LIMIT 1""", (wall_time - 10, wall_time)).fetchone()
        return row

    def outcome(self, ticker, attempts, *, market=None, raw=None, error=None, delay=None):
        observed_at, now = iso(), time.time()
        with self.connect() as connection:
            if market is not None:
                evidence = {**market, "official_detail": raw, "outcome_received_at": observed_at}
                self._market(connection, evidence, observed_at)
            wait = min(600, 15 * 2 ** min(attempts, 6)) if delay is None else max(15, delay)
            connection.execute("UPDATE markets SET attempts=attempts+1,next_attempt=?,last_error=? WHERE ticker=?",
                               (now + wait, error, ticker))

    def end(self, session_id, summary):
        with self.connect() as connection:
            connection.execute("UPDATE sessions SET ended_at=?,summary=? WHERE session_id=?",
                               (iso(), json.dumps(summary, allow_nan=False), session_id))


class SampleFeatures:
    """Simple reproducible features from the sampled quotes; no trading effects."""
    def __init__(self):
        self.samples = deque(maxlen=310)
        self.update = None

    def snapshot(self, snapshot, book, now, wall_time):
        feed = snapshot["feeds"].get("kraken", {})
        data = feed.get("data", {})
        bid, ask = _num(data.get("bid")), _num(data.get("ask"))
        fresh = (_fresh(feed) and feed.get("transport") == "websocket" and
                 data.get("symbol") == "BTC/USD" and bid is not None and ask is not None and 0 < bid <= ask)
        midpoint = bid + (ask - bid) / 2 if fresh else None
        if self.samples and (now < self.samples[-1][0] or now - self.samples[-1][0] > 15):
            self.samples.clear()
            self.update = None
        if fresh and feed.get("updates") != self.update:
            self.samples.append((now, midpoint))
            self.update = feed.get("updates")
        while self.samples and now - self.samples[0][0] > 305:
            self.samples.popleft()

        def old(seconds):
            return next((price for stamp, price in reversed(self.samples) if stamp <= now - seconds), None)

        result = {"quote_fresh": fresh, "midpoint": midpoint}
        for seconds in (5, 20, 60):
            price = old(seconds)
            result[f"momentum_{seconds}s_bps"] = (math.log(midpoint / price) * 10000
                                                   if midpoint is not None and price is not None else None)
        start = old(60)
        window = [(stamp, price) for stamp, price in self.samples if stamp >= now - 61]
        movement = sum(abs(b[1] - a[1]) for a, b in zip(window, window[1:]))
        result["efficiency_60s"] = (abs(midpoint - window[0][1]) / movement if movement else 0.) if fresh and start is not None else None
        squared = sum(math.log(b[1] / a[1]) ** 2 for a, b in zip(window, window[1:]))
        span = window[-1][0] - window[0][0] if len(window) > 1 else 0
        result["realized_volatility_bps_sqrt_second"] = math.sqrt(squared / span) * 10000 if fresh and start is not None and span > 0 else None
        strike = _num(book.get("strike_price"))
        result["distance_from_floor_percent"] = ((midpoint / strike - 1) * 100 if midpoint is not None
            and strike is not None and strike > 0 and book.get("strike_source") == "kalshi.floor_strike" else None)
        close = _timestamp(book.get("close_time"))
        elapsed = wall_time - (close - 900) if close is not None else None
        result.update(elapsed_seconds=elapsed, remaining_seconds=close - wall_time if close is not None else None,
                      within_preferred_5_to_13_minutes=300 <= elapsed < 780 if elapsed is not None else None)
        return result


class ResearchRecorder:
    def __init__(self, store, path, *, paper_path=None, interval=INTERVAL, queue_limit=QUEUE_LIMIT,
                 outcome_interval=5, max_bytes=DEFAULT_MAX_BYTES, decision_traces=True):
        if isinstance(queue_limit, bool) or not isinstance(queue_limit, int) or queue_limit < 1:
            raise ValueError("Research queue capacity must be a positive integer")
        if paper_path is not None and Path(path).resolve() == Path(paper_path).resolve():
            raise ValueError("Research and paper journals must use different files")
        self.store, self.database = store, ResearchDB(path, max_bytes=max_bytes)
        self.interval = interval
        self.outcome_interval = outcome_interval
        self.session_id = uuid.uuid4().hex
        self.started_at, self.started_mono = iso(), time.monotonic()
        self.paper_path = str(paper_path) if paper_path is not None else None
        self.queue = asyncio.Queue(maxsize=queue_limit)
        self.stop_event, self.ready = asyncio.Event(), asyncio.Event()
        self.sequence = self.written = self.dropped = 0
        self.dropped_by_kind = Counter()
        self.reserved_capacity = max(1, min(16, queue_limit // 4))
        self.trace_capacity = max(0, queue_limit - 2 * self.reserved_capacity)
        self.ordinary_capacity = max(0, queue_limit - self.reserved_capacity)
        self.decision_traces = decision_traces
        self.decision_encoder = DecisionEncoder()
        self.pending_bootstrap = None
        self.last_written_at = self.error = self.outcome_error = None
        self.tasks = []
        self.features = SampleFeatures()

    def status(self):
        return {"enabled": True, "status": "error" if self.error else "recording" if self.last_written_at else "starting",
                "session_id": self.session_id, "records_written": self.written,
                "last_written_at": self.last_written_at, "dropped_records": self.dropped,
                "queue_depth": self.queue.qsize(), "error": self.error,
                "outcome_error": self.outcome_error, "interval_seconds": self.interval,
                "dropped_records_by_kind": dict(self.dropped_by_kind),
                "decision_traces": self.decision_traces, "decision_trace_format": FORMAT,
                "decision_trace_sequence": self.decision_encoder.sequence}

    def enqueue(self, kind, payload, *, ticker=None):
        self.sequence += 1
        record = {"session_id": self.session_id, "sequence": self.sequence, "kind": kind,
                  "captured_at": iso(), "ticker": ticker, "payload": payload}
        capacity = (self.trace_capacity if kind == "decision" else self.ordinary_capacity
                    if kind == "snapshot" else self.queue.maxsize)
        if self.queue.qsize() >= capacity:
            self.dropped += 1
            self.dropped_by_kind[kind] += 1
            return False
        try:
            self.queue.put_nowait(record)
            return True
        except asyncio.QueueFull:
            self.dropped += 1
            self.dropped_by_kind[kind] += 1
            return False

    def initialize_decisions(self, engine, *, reason="startup"):
        """A bounded starting account checkpoint, before startup/settings steps."""
        if not self.decision_traces:
            return
        try:
            bootstrap_id = self.decision_encoder.begin()
            self.pending_bootstrap = finite_copy({
                "format": FORMAT, "bootstrap_id": bootstrap_id, "reason": reason,
                "before_trace_sequence": self.decision_encoder.sequence + 1,
                "state": engine.dump_state(), "config_revision": engine.config_revision,
                "coverage": "Account checkpoint plus engine-step inputs; not raw exchange ticks",
                "replay_scope": "Replay requires uninterrupted inputs from a cold startup; full anchors resync context, not feature history",
            })
            self._enqueue_bootstrap()
        except Exception:
            self.dropped += 1
            self.dropped_by_kind["decision_bootstrap"] += 1
            self.decision_encoder.invalidate()
            LOG.exception("Could not initialize research decision trace")

    def _enqueue_bootstrap(self):
        if self.pending_bootstrap is not None and self.enqueue("decision_bootstrap", self.pending_bootstrap):
            self.pending_bootstrap = None
        return self.pending_bootstrap is None

    def record_decision(self, engine, events):
        """Nonblocking decision evidence; a fill requires a separate commit record."""
        if not self.decision_traces:
            return
        trace_before, record_before = self.decision_encoder.sequence, self.sequence
        try:
            if self.decision_encoder.bootstrap_id == 0:
                # Runtime normally supplies the pre-step checkpoint. Do not
                # invent one from an engine that has already attempted a fill.
                raise ValueError("Decision trace requires a pre-step bootstrap")
            frame = decision_frame(engine, events)
            payload = self.decision_encoder.encode(frame)
            if not self._enqueue_bootstrap():
                self.sequence += 1
                self.dropped += 1
                self.dropped_by_kind["decision"] += 1
                self.decision_encoder.invalidate()
            elif not self.enqueue("decision", payload, ticker=frame["book"]["ticker"]):
                self.decision_encoder.invalidate()
        except Exception:
            # Even a frame-copy failure means an actual engine observation is
            # absent. Reserve its sequence so the next full anchor exposes the
            # gap without depending on an eventual session-end summary.
            if self.decision_encoder.sequence == trace_before:
                self.decision_encoder.sequence += 1
            if self.sequence == record_before:
                self.sequence += 1
            self.dropped += 1
            self.dropped_by_kind["decision"] += 1
            self.decision_encoder.invalidate()
            LOG.exception("Could not record research decision; paper engine continues")

    def record_fill(self, engine, events):
        # Called only after the authoritative paper journal transaction commits.
        try:
            context = engine._condition_context or {}
            for event in events:
                self.enqueue("paper_fill", copy.deepcopy({"event": event,
                    "decision_inputs": context, "paper_cash_after_commit": engine.cash,
                    "recorded_after_paper_commit": True}), ticker=event.get("ticker"))
        except Exception:
            self.dropped += 1
            self.dropped_by_kind["paper_fill"] += 1
            LOG.exception("Could not enqueue research fill; authoritative paper journal is intact")

    async def start(self):
        self.tasks = [asyncio.create_task(self.writer(), name="research-writer"),
                      asyncio.create_task(self.sampler(), name="research-sampler"),
                      asyncio.create_task(self.outcomes(), name="research-outcomes")]

    async def wait(self, seconds):
        try:
            await asyncio.wait_for(self.stop_event.wait(), seconds)
        except TimeoutError:
            pass

    async def sampler(self):
        while not self.stop_event.is_set():
            started = time.monotonic()
            try:
                snapshot, book = self.store.snapshot(), self.store.book_snapshot()
                snapshot.pop("research", None)
                snapshot["paper"] = {key: value for key, value in snapshot["paper"].items() if key != "events"}
                snapshot["settlement_outlook"] = {key: value for key, value in snapshot["settlement_outlook"].items() if key != "history"}
                snapshot.update(schema=SCHEMA, sampled_at_mono=started,
                                session_elapsed_seconds=started - self.started_mono,
                                dropped_records_total=self.dropped, book=book,
                                features=self.features.snapshot(snapshot, book, started, time.time()),
                                coverage={"sampling": "normalized_1s", "raw_exchange_ticks": False,
                                          "live_settlement_index": False,
                                          "full_depth_ticker": book.get("ticker"),
                                          "model_id": "spot-normal-v1"})
                self.enqueue("snapshot", copy.deepcopy(snapshot), ticker=book.get("ticker"))
            except Exception:
                self.error = "Research sampling failed; see the terminal log"
                LOG.exception(self.error)
            await self.wait(max(.01, self.interval - (time.monotonic() - started)))

    async def writer(self):
        metadata = {"schema": SCHEMA, "started_at": self.started_at, "interval_seconds": self.interval,
                    "series": self.store.settings.series, "symbol": self.store.settings.symbol,
                    "kalshi_poll_seconds": self.store.settings.poll_seconds,
                    "max_database_bytes": self.database.max_bytes,
                    "paper_journal": self.paper_path, "index_feed_available": False,
                    "decision_trace": {"enabled": self.decision_traces, "format": FORMAT,
                                       "cadence": "each actual paper engine step, normally about 100ms",
                                       "anchor_seconds": ANCHOR_SECONDS,
                                       "raw_exchange_ticks": False},
                    "model": {"id": "spot-normal-v1", "volatility_floor": VOLATILITY_FLOOR,
                              "index_uncertainty": INDEX_UNCERTAINTY, "momentum_seconds": MOMENTUM_SECONDS}}
        while not self.ready.is_set():
            try:
                await asyncio.to_thread(self.database.initialize, self.session_id, metadata)
                self.ready.set()
                self.error = None
            except (OSError, sqlite3.Error, ValueError) as error:
                self.error = f"Research database unavailable: {error}"
                LOG.error(self.error)
                if self.stop_event.is_set():
                    return
                await self.wait(5)
        batch = []
        while not self.stop_event.is_set() or batch or not self.queue.empty():
            if not batch:
                try:
                    batch.append(await asyncio.wait_for(self.queue.get(), .25))
                except TimeoutError:
                    continue
                while len(batch) < 10 and not self.queue.empty():
                    batch.append(self.queue.get_nowait())
                deadline = asyncio.get_running_loop().time() + .2
                while len(batch) < 50 and not self.stop_event.is_set():
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        break
                    try:
                        batch.append(await asyncio.wait_for(self.queue.get(), remaining))
                    except TimeoutError:
                        break
                while len(batch) < 50 and not self.queue.empty():
                    batch.append(self.queue.get_nowait())
            try:
                await asyncio.to_thread(self.database.write, batch)
                # A retried transaction may already be committed. Count the
                # acknowledged batch once even when INSERT OR IGNORE dedupes it.
                self.written += len(batch)
                self.last_written_at, self.error = iso(), None
                batch.clear()
            except (OSError, sqlite3.Error, ValueError, TypeError) as error:
                self.error = f"Research save failed: {error}"
                LOG.error(self.error)
                if self.stop_event.is_set():
                    return
                await self.wait(2)
        try:
            await asyncio.to_thread(self.database.end, self.session_id,
                {"records_written": self.written, "dropped_records": self.dropped,
                 "dropped_records_by_kind": dict(self.dropped_by_kind),
                 "last_sequence": self.sequence, "outcome_error": self.outcome_error})
        except (OSError, sqlite3.Error):
            LOG.exception("Research session end could not be saved")

    async def outcomes(self):
        timeout = aiohttp.ClientTimeout(total=5, connect=3)
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True,
                headers={"User-Agent": "kalshi-kraken-feed/research"}) as session:
            while not self.stop_event.is_set():
                if self.ready.is_set():
                    try:
                        due = await asyncio.to_thread(self.database.due_market, time.time())
                        if due:
                            ticker, attempts = due
                            error = market = raw = delay = None
                            try:
                                url = self.store.settings.kalshi_url.rstrip("/") + "/" + quote(ticker, safe="")
                                async with session.get(url) as response:
                                    if response.status == 429:
                                        delay = retry_after_seconds(response.headers.get("Retry-After"))
                                    response.raise_for_status()
                                    payload = await response.json()
                                raw = payload.get("market") if isinstance(payload, dict) else None
                                market = normalize_kalshi({"markets": [raw]}, 1)[0]
                                if market["ticker"] != ticker:
                                    raise ValueError("Outcome response does not match the requested contract")
                            except (aiohttp.ClientError, TimeoutError, ValueError, KeyError) as failure:
                                market = raw = None
                                error = f"{type(failure).__name__}: official result not available"
                            await asyncio.to_thread(self.database.outcome, ticker, attempts,
                                market=market, raw=raw, error=error, delay=delay)
                            self.outcome_error = error
                    except (OSError, sqlite3.Error, ValueError) as error:
                        self.outcome_error = f"Outcome collection unavailable: {error}"
                await self.wait(self.outcome_interval)

    async def close(self):
        self.stop_event.set()
        try:
            await asyncio.wait_for(asyncio.gather(*self.tasks), 8)
        except TimeoutError:
            LOG.error("Research shutdown timed out; queued records may be missing")

"""Asynchronous paper simulation with an atomic, local SQLite trade journal."""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from pathlib import Path

from .paper import PaperConfig, PaperEngine

LOG = logging.getLogger(__name__)


class PaperJournal:
    def __init__(self, path: Path):
        self.path = Path(path)

    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=2)
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("CREATE TABLE IF NOT EXISTS paper_state "
                           "(id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL, state TEXT NOT NULL)")
        connection.execute("CREATE TABLE IF NOT EXISTS paper_events "
                           "(event_id INTEGER PRIMARY KEY, payload TEXT NOT NULL)")
        connection.commit()
        return connection

    def load(self) -> tuple[dict | None, int]:
        connection = self._connect()
        try:
            row = connection.execute("SELECT state, revision FROM paper_state WHERE id=1").fetchone()
            return (json.loads(row[0]), row[1]) if row else (None, 0)
        finally:
            connection.close()

    def save(self, state: dict, events: list[dict], revision: int) -> int:
        document = json.dumps(state, allow_nan=False, separators=(",", ":"))
        connection = self._connect()
        try:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute("SELECT revision FROM paper_state WHERE id=1").fetchone()
                actual = row[0] if row else 0
                if actual != revision:
                    raise RuntimeError("Another application updated this paper journal; use one instance per journal")
                next_revision = revision + 1
                connection.execute("INSERT INTO paper_state(id,revision,state) VALUES(1,?,?) "
                                   "ON CONFLICT(id) DO UPDATE SET revision=excluded.revision,state=excluded.state",
                                   (next_revision, document))
                for event in events:
                    connection.execute("INSERT INTO paper_events(event_id,payload) VALUES(?,?)",
                                       (event["id"], json.dumps(event, allow_nan=False, separators=(",", ":"))))
            return next_revision
        finally:
            connection.close()

    def recent_events(self, limit: int = 50) -> list[dict]:
        connection = self._connect()
        try:
            rows = connection.execute("SELECT payload FROM paper_events ORDER BY event_id DESC LIMIT ?",
                                      (max(1, min(200, limit)),)).fetchall()
            return [json.loads(row[0]) for row in rows]
        finally:
            connection.close()


def paused_snapshot(message: str, previous: dict | None = None):
    return {**(previous or {}), "enabled": True, "mode": "paper", "status": "paused", "message": message}


async def run_paper(store, journal: PaperJournal, config: PaperConfig):
    """Never perform disk I/O in the feed or render loops; publish fills after commit."""
    try:
        state, revision = await asyncio.to_thread(journal.load)
        engine = PaperEngine(config, state)
        if state is None:
            revision = await asyncio.to_thread(journal.save, engine.dump_state(), [], revision)
    except (OSError, sqlite3.Error, ValueError, RuntimeError):
        LOG.exception("Paper journal could not be loaded; paper trading is paused")
        store.paper = paused_snapshot("Paper journal unavailable; check the server log. Existing data was preserved.")
        store.revision += 1
        return
    store.paper_engine = engine
    pending = None
    while True:
        try:
            if pending is None:
                # This snapshot uses actual current ages, even when a feed has stopped updating.
                events = engine.step(store.snapshot()["feeds"], store.book_snapshot(),
                                     now=time.monotonic(), wall_time=time.time())
                if events:
                    pending = (engine.dump_state(), events)
            if pending is not None:
                revision = await asyncio.to_thread(journal.save, pending[0], pending[1], revision)
                pending = None
            current = engine.snapshot()
            if current != store.paper:
                store.paper = current
                store.revision += 1
            await asyncio.sleep(0.1)
        except (OSError, sqlite3.Error, ValueError, RuntimeError):
            LOG.exception("Paper simulation paused; feeds remain available")
            store.paper = paused_snapshot("Paper trading paused: journal or simulation error; see server log.", store.paper)
            store.revision += 1
            if pending is None:
                # An unexpected simulation exception is not retried as if nothing happened.
                return
            # Freeze all new decisions until the exact outstanding transaction can be committed.
            await asyncio.sleep(5)

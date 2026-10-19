"""Asynchronous paper simulation with an atomic, local SQLite trade journal."""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import math
import sqlite3
import time
from pathlib import Path

from .paper import PaperConfig, PaperEngine
from .strategy_settings import apply_public_settings, public_settings

LOG = logging.getLogger(__name__)


class SettingsUnavailable(RuntimeError):
    pass


class SettingsConflict(ValueError):
    pass


class PaperControl:
    """Serialize settings changes with simulated fills, through the engine owner."""

    def __init__(self, config: PaperConfig):
        self.defaults = config
        self.engine = None
        self.available = False
        self.requests = asyncio.Queue(maxsize=4)

    def document(self):
        engine = self.engine
        return {"settings": public_settings(engine.config if engine else self.defaults),
                "revision": getattr(engine, "config_revision", 0), "available": self.available}

    def attach(self, engine):
        self.engine = engine
        self.available = True

    def disable(self, message):
        self.available = False
        while not self.requests.empty():
            _, _, future, _ = self.requests.get_nowait()
            if not future.done():
                future.set_exception(SettingsUnavailable(message))

    async def change(self, values, expected_revision, *, apply_exit_controls_to_position=None):
        if not self.available:
            raise SettingsUnavailable("Paper settings are unavailable while the engine is paused or starting")
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 0:
            raise ValueError("A nonnegative settings revision is required; reload settings first")
        if apply_exit_controls_to_position is not None:
            identity = apply_exit_controls_to_position
            if (not isinstance(identity, dict) or set(identity) != {"ticker", "opened_at"}
                    or not isinstance(identity["ticker"], str) or not identity["ticker"]
                    or isinstance(identity["opened_at"], bool) or not isinstance(identity["opened_at"], (int, float))):
                raise ValueError("Identify the open trade by its ticker and numeric opened_at time")
            try:
                valid_time = math.isfinite(identity["opened_at"]) and identity["opened_at"] >= 0
            except OverflowError:
                valid_time = False
            if not valid_time:
                raise ValueError("Open trade time must be finite and nonnegative")
        future = asyncio.get_running_loop().create_future()
        try:
            self.requests.put_nowait((values, expected_revision, future, apply_exit_controls_to_position))
        except asyncio.QueueFull:
            raise SettingsUnavailable("A settings update is already pending; try again shortly")
        return await future


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
    control = store.paper_control or PaperControl(config)
    store.paper_control = control
    try:
        state, revision = await asyncio.to_thread(journal.load)
        engine = PaperEngine(config, state)
        if state is None or state.get("config") != engine.dump_state().get("config"):
            revision = await asyncio.to_thread(journal.save, engine.dump_state(), [], revision)
    except (OSError, sqlite3.Error, ValueError, RuntimeError):
        LOG.exception("Paper journal could not be loaded; paper trading is paused")
        store.paper = paused_snapshot("Paper journal unavailable; check the server log. Existing data was preserved.")
        control.disable("Paper journal unavailable")
        store.revision += 1
        return
    store.paper_engine = engine
    control.attach(engine)
    pending = None
    active_request = None
    try:
        while True:
            try:
                if pending is None and not control.requests.empty():
                    values, expected, future, position_identity = control.requests.get_nowait()
                    active_request = future
                    if future.cancelled():
                        active_request = None
                        continue
                    try:
                        if expected != engine.config_revision:
                            raise SettingsConflict("Settings changed in another tab; reload before saving again")
                        updated = apply_public_settings(engine.config, values)
                        candidate = copy.deepcopy(engine)
                        candidate.set_config(updated)
                        if position_identity is not None:
                            if (candidate.position is None
                                    or candidate.position["ticker"] != position_identity["ticker"]
                                    or candidate.position["opened_at"] != position_identity["opened_at"]):
                                raise SettingsConflict("The open trade changed or closed; reload before applying exit controls")
                            candidate.apply_exit_controls_to_position(**position_identity, wall_time=time.time())
                    except ValueError as error:
                        if not future.done():
                            future.set_exception(error)
                        active_request = None
                        continue
                    try:
                        if candidate.dump_state() != engine.dump_state():
                            revision = await asyncio.to_thread(journal.save, candidate.dump_state(), [], revision)
                    except (OSError, sqlite3.Error, ValueError, RuntimeError) as error:
                        LOG.exception("Paper settings save could not be confirmed; trading is paused")
                        message = "Settings save could not be confirmed; restart the app to check the journal before trading resumes"
                        store.paper = paused_snapshot(message, store.paper)
                        store.revision += 1
                        if not future.done():
                            future.set_exception(SettingsUnavailable(message))
                        active_request = None
                        control.disable(message)
                        return
                    engine = candidate
                    store.paper_engine = engine
                    control.attach(engine)
                    store.paper = engine.snapshot()
                    store.revision += 1
                    if not future.done():
                        future.set_result({"status": "saved", "applied_to_position": position_identity is not None,
                                           **control.document()})
                    active_request = None
                    # The next decision uses the committed rules; saving itself never places an order.
                    await asyncio.sleep(0.1)
                    continue
                if pending is None:
                    # This snapshot uses actual current ages, even when a feed has stopped updating.
                    events = engine.step(store.snapshot()["feeds"], store.book_snapshot(),
                                         now=time.monotonic(), wall_time=time.time())
                    if events:
                        pending = (engine.dump_state(), events)
                if pending is not None:
                    revision = await asyncio.to_thread(journal.save, pending[0], pending[1], revision)
                    pending = None
                    control.attach(engine)
                current = engine.snapshot()
                if current != store.paper:
                    store.paper = current
                    store.revision += 1
                await asyncio.sleep(0.1)
            except (OSError, sqlite3.Error, ValueError, RuntimeError):
                LOG.exception("Paper simulation paused; feeds remain available")
                store.paper = paused_snapshot("Paper trading paused: journal or simulation error; see server log.", store.paper)
                control.disable("Paper trading is paused after a journal or simulation error")
                store.revision += 1
                if pending is None:
                    # An unexpected simulation exception is not retried as if nothing happened.
                    return
                # Freeze all new decisions until the exact outstanding transaction can be committed.
                await asyncio.sleep(5)
    finally:
        if active_request is not None and not active_request.done():
            active_request.set_exception(SettingsUnavailable(
                "The app stopped during a settings save; reload after restart to check the saved settings"))
        control.disable("The paper engine has stopped")

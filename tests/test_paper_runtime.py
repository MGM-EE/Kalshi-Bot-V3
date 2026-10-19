"""Local paper journal/runtime integration. No exchange traffic or real orders."""
import asyncio
import copy
import sqlite3
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from aiohttp.test_utils import TestClient, TestServer

from feedboard.core import Settings, Store
from feedboard.paper import PaperConfig, PaperEngine
from feedboard.paper_runtime import PaperJournal, run_paper
from feedboard.web import JOURNAL, create_app


TICKER = "KXBTC15M-RUNTIME-TEST"


def synthetic_inputs(second, epoch):
    close = datetime.fromtimestamp(epoch + 900, timezone.utc).isoformat()
    mid = 65000 + second * 4
    feeds = {
        "kalshi": {"status": "live", "age_ms": 0},
        "kraken": {"status": "live", "age_ms": 0, "updates": second + 1,
                   "transport": "websocket", "data": {
                       "symbol": "BTC/USD", "bid": mid - .5, "ask": mid + .5,
                       "flow": {"buy_usd": 9000, "sell_usd": 1000,
                                "large_buy_usd": 2000, "large_sell_usd": 0,
                                "count": 20, "age_ms": 0, "window_seconds": 20}}},
    }
    book = {"ticker": TICKER, "status": "live", "age_ms": 0,
            "version": second + 1, "yes": [[.48, 20]], "no": [[.50, 20]],
            "strike_price": 66000, "strike_source": "kalshi.floor_strike",
            "close_time": close, "market_status": "open"}
    return feeds, book


def open_account():
    """Generate a real engine entry from explicitly synthetic fresh inputs."""
    engine = PaperEngine()
    epoch = time.time()
    for second in range(21):
        feeds, book = synthetic_inputs(second, epoch)
        engine.step(feeds, book, now=second, wall_time=epoch + second)
    if engine.position is None:
        raise AssertionError("synthetic fixture did not enter")
    return engine


def executable_exit(store):
    store.book = {"ticker": TICKER, "status": "live", "version": 22,
                  "yes": [[.95, 20]], "no": [[.04, 20]], "settlement_result": None,
                  "market_status": "open"}
    store.book_mono = time.monotonic()


class LocalAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "paper.sqlite3"

    async def until(self, predicate):
        async with asyncio.timeout(5):
            while not predicate():
                await asyncio.sleep(.01)

    async def stop(self, task):
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


class JournalTests(LocalAsyncTests):
    async def test_state_and_events_survive_reopen_with_revision(self):
        engine = open_account()
        state = engine.dump_state()
        journal = PaperJournal(self.path)
        self.assertEqual(journal.load(), (None, 0))
        self.assertEqual(journal.save(state, state["events"], 0), 1)
        reopened = PaperJournal(self.path)
        self.assertEqual(reopened.load(), (state, 1))
        self.assertEqual(reopened.recent_events(), list(reversed(state["events"])))

    async def test_stale_writer_cannot_overwrite_account_or_insert_events(self):
        state = open_account().dump_state()
        first = PaperJournal(self.path)
        second = PaperJournal(self.path)
        first.save(state, state["events"], 0)
        stale_state, stale_revision = second.load()
        newer_state = {**state, "cash": state["cash"] + 10}
        event = {**state["events"][0], "id": 2}
        first.save(newer_state, [event], 1)
        with self.assertRaisesRegex(RuntimeError, "Another application"):
            second.save({**stale_state, "cash": 1}, [{**event, "id": 3}], stale_revision)
        self.assertEqual(first.load(), (newer_state, 2))
        self.assertEqual([e["id"] for e in first.recent_events()], [2, 1])

    async def test_duplicate_event_rolls_back_state_revision_and_prior_inserts(self):
        state = open_account().dump_state()
        journal = PaperJournal(self.path)
        journal.save(state, state["events"], 0)
        duplicate = state["events"][0]
        event = {**duplicate, "id": 2}
        with self.assertRaises(sqlite3.IntegrityError):
            journal.save({**state, "cash": 17}, [event, duplicate], 1)
        self.assertEqual(journal.load(), (state, 1))
        self.assertEqual(journal.recent_events(), [duplicate])
        # The failed transaction consumed neither the event ID nor the revision.
        self.assertEqual(journal.save(state, [event], 1), 2)

    async def test_nonfinite_event_rolls_back_other_valid_changes(self):
        state = open_account().dump_state()
        journal = PaperJournal(self.path)
        journal.save(state, state["events"], 0)
        event = {**state["events"][0], "id": 2, "price": float("nan")}
        with self.assertRaises(ValueError):
            journal.save({**state, "cash": 17}, [event], 1)
        self.assertEqual(journal.load(), (state, 1))
        self.assertEqual(journal.recent_events(), state["events"])

    async def test_history_is_newest_first_and_bounded(self):
        journal = PaperJournal(self.path)
        events = [{"id": index} for index in range(1, 206)]
        journal.save({"synthetic": True}, events, 0)
        self.assertEqual(journal.recent_events(2), [{"id": 205}, {"id": 204}])
        self.assertEqual(len(journal.recent_events(10000)), 200)
        self.assertEqual(journal.recent_events(0), [{"id": 205}])


class RuntimeTests(LocalAsyncTests):
    async def test_first_real_entry_reaches_snapshot_after_its_atomic_commit(self):
        engine = PaperEngine()
        epoch, mono = time.time() - 20, time.monotonic() - 20
        for second in range(20):
            feeds, book = synthetic_inputs(second, epoch)
            self.assertEqual(engine.step(feeds, book, now=mono + second,
                                         wall_time=epoch + second), [])
        self.assertIsNone(engine.position)
        initial = engine.dump_state()
        PaperJournal(self.path).save(initial, [], 0)
        feeds, book = synthetic_inputs(20, epoch)
        store = Store(Settings())
        store.update("kalshi", {"series": "KXBTC15M", "markets": []}, "rest")
        store.update("kraken", feeds["kraken"]["data"], "websocket")
        store.book, store.book_mono = book, time.monotonic()
        before_commit = []

        class ObservingJournal(PaperJournal):
            def save(self, state, events, revision):
                before_commit.append(copy.deepcopy(store.paper))
                return super().save(state, events, revision)

        # Supply an actually warmed real engine so the integration test needs no clock overrides.
        with patch("feedboard.paper_runtime.PaperEngine", return_value=engine):
            task = asyncio.create_task(run_paper(store, ObservingJournal(self.path), PaperConfig()))
            try:
                await self.until(lambda: (store.paper.get("last_event") or {}).get("id") == 1)
                self.assertEqual(len(before_commit), 1)
                self.assertFalse(before_commit[0]["enabled"])
                committed, revision = PaperJournal(self.path).load()
                self.assertEqual(revision, 2)
                self.assertEqual(committed["events"], PaperJournal(self.path).recent_events())
                self.assertEqual(store.paper["last_event"]["type"], "entry")
                self.assertEqual(store.paper["position"]["side"], "up")
                self.assertEqual(committed["position"]["contracts"], 5)
                self.assertEqual(store.paper["cash"], round(committed["cash"], 4))
                self.assertLess(committed["cash"], 1000)
            finally:
                await self.stop(task)

    async def test_restart_restores_cash_position_and_ids_without_resetting_account(self):
        state = open_account().dump_state()
        journal = PaperJournal(self.path)
        journal.save(state, state["events"], 0)
        store = Store(Settings())
        task = asyncio.create_task(run_paper(store, journal, PaperConfig(initial_cash=20)))
        try:
            await self.until(lambda: store.paper.get("enabled"))
            self.assertEqual(store.paper["status"], "holding")
            self.assertEqual(store.paper["cash"], round(state["cash"], 4))
            self.assertEqual(store.paper["position"]["contracts"], 5)
            self.assertEqual(store.paper["last_event"]["id"], 1)
            self.assertEqual(store.paper_engine.dump_state(), state)
            self.assertEqual(journal.load(), (state, 1))
        finally:
            await self.stop(task)

    async def test_invalid_saved_state_pauses_and_preserves_original(self):
        journal = PaperJournal(self.path)
        damaged = {"schema": 999, "cash": 73, "position": "preserve this evidence"}
        journal.save(damaged, [{"id": 9, "original": True}], 0)
        store = Store(Settings())
        with self.assertLogs("feedboard.paper_runtime", level="ERROR"):
            await run_paper(store, journal, PaperConfig())
        self.assertEqual(store.paper["status"], "paused")
        self.assertIsNone(store.paper_engine)
        self.assertEqual(journal.load(), (damaged, 1))
        self.assertEqual(journal.recent_events(), [{"id": 9, "original": True}])

    async def test_unreadable_database_is_not_replaced_by_a_new_account(self):
        original = b"This is a damaged database that must be preserved."
        self.path.write_bytes(original)
        store = Store(Settings())
        with self.assertLogs("feedboard.paper_runtime", level="ERROR"):
            await run_paper(store, PaperJournal(self.path), PaperConfig())
        self.assertEqual(store.paper["status"], "paused")
        self.assertEqual(self.path.read_bytes(), original)

    async def test_disk_work_is_off_thread_and_fill_is_published_only_after_commit(self):
        original = open_account().dump_state()
        PaperJournal(self.path).save(original, original["events"], 0)
        store = Store(Settings())
        executable_exit(store)
        loop = asyncio.get_running_loop()
        save_started = asyncio.Event()
        release = threading.Event()
        thread_ids = []

        class BlockingJournal(PaperJournal):
            def load(self):
                thread_ids.append(threading.get_ident())
                return super().load()

            def save(self, state, events, revision):
                thread_ids.append(threading.get_ident())
                loop.call_soon_threadsafe(save_started.set)
                if not release.wait(5):
                    raise TimeoutError("test did not release blocked journal")
                return super().save(state, events, revision)

        task = asyncio.create_task(run_paper(store, BlockingJournal(self.path), PaperConfig()))
        try:
            await asyncio.wait_for(save_started.wait(), 5)
            self.assertTrue(all(identity != threading.get_ident() for identity in thread_ids))
            self.assertFalse(store.paper["enabled"])
            # Quotes and the event loop remain usable while the disk operation is blocked.
            store.update("kraken", {"symbol": "BTC/USD", "last": 65000}, "websocket")
            self.assertEqual(store.snapshot()["feeds"]["kraken"]["data"]["last"], 65000)
            self.assertEqual(PaperJournal(self.path).load(), (original, 1))
            release.set()
            await self.until(lambda: store.paper.get("last_event", {}).get("id") == 2)
            committed, revision = PaperJournal(self.path).load()
            self.assertEqual(revision, 2)
            self.assertIsNone(committed["position"])
            self.assertIsNone(store.paper["position"])
            self.assertEqual(store.paper["cash"], round(committed["cash"], 4))
            self.assertEqual([e["id"] for e in PaperJournal(self.path).recent_events()], [2, 1])
        finally:
            release.set()
            await self.stop(task)

    async def test_failed_commit_freezes_decisions_then_retries_exact_transaction(self):
        original = open_account().dump_state()
        PaperJournal(self.path).save(original, original["events"], 0)
        engine = PaperEngine(state=original)
        engine.step = Mock(wraps=engine.step)
        store = Store(Settings())
        executable_exit(store)
        real_sleep = asyncio.sleep
        retry_waiting, retry_release = asyncio.Event(), asyncio.Event()
        retry_started = asyncio.Event()
        commit_release = threading.Event()
        loop = asyncio.get_running_loop()
        attempts = []

        class FailingOnceJournal(PaperJournal):
            def save(self, state, events, revision):
                attempts.append(copy.deepcopy((state, events, revision)))
                if len(attempts) == 1:
                    raise sqlite3.OperationalError("synthetic disk busy")
                loop.call_soon_threadsafe(retry_started.set)
                if not commit_release.wait(5):
                    raise TimeoutError("test did not release retry")
                return super().save(state, events, revision)

        async def controlled_sleep(seconds):
            if seconds == 5:
                retry_waiting.set()
                await retry_release.wait()
            else:
                await real_sleep(seconds)

        with patch("feedboard.paper_runtime.PaperEngine", return_value=engine), \
                patch("feedboard.paper_runtime.asyncio.sleep", controlled_sleep), \
                self.assertLogs("feedboard.paper_runtime", level="ERROR"):
            task = asyncio.create_task(run_paper(store, FailingOnceJournal(self.path), PaperConfig()))
            try:
                await asyncio.wait_for(retry_waiting.wait(), 5)
                self.assertEqual(store.paper["status"], "paused")
                self.assertNotIn("last_event", store.paper)
                self.assertEqual(engine.step.call_count, 1)
                self.assertEqual(PaperJournal(self.path).load(), (original, 1))
                # New market input cannot cause a new decision while a fill is uncommitted.
                store.book["version"] += 1
                retry_release.set()
                await asyncio.wait_for(retry_started.wait(), 5)
                self.assertEqual(engine.step.call_count, 1)
                self.assertEqual(attempts[0], attempts[1])
                commit_release.set()
                async with asyncio.timeout(5):
                    while store.paper.get("last_event", {}).get("id") != 2:
                        await real_sleep(.01)
                self.assertEqual(store.paper["status"], "watching")
                self.assertEqual([e["id"] for e in PaperJournal(self.path).recent_events()], [2, 1])
            finally:
                commit_release.set()
                retry_release.set()
                await self.stop(task)


class PaperHistoryHttpTests(LocalAsyncTests):
    async def client_for_journal(self):
        journal = PaperJournal(self.path)
        state = open_account().dump_state()
        events = [{**state["events"][0], "id": index} for index in range(1, 206)]
        journal.save(state, events, 0)
        app = create_app(run_feeds=False)
        # Attach the journal without starting a simulation: these are read-only API checks.
        app[JOURNAL] = journal
        client = await self.enterAsyncContext(TestClient(TestServer(app)))
        return client, journal, state, events

    async def test_get_history_is_bounded_newest_first_and_read_only(self):
        client, journal, state, events = await self.client_for_journal()
        response = await client.get("/api/paper/trades?limit=2")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(await response.json(), {"mode": "paper", "events": events[-2:][::-1]})
        response = await client.get("/api/paper/trades")
        self.assertEqual(len((await response.json())["events"]), 50)
        response = await client.get("/api/paper/trades?limit=200")
        self.assertEqual(len((await response.json())["events"]), 200)
        for method in (client.post, client.put, client.delete):
            self.assertEqual((await method("/api/paper/trades")).status, 405)
        self.assertEqual(journal.load(), (state, 1))
        self.assertEqual(journal.recent_events(200), events[-200:][::-1])

    async def test_invalid_limits_are_rejected_without_touching_account(self):
        client, journal, state, _ = await self.client_for_journal()
        for value in ("0", "-1", "201", "1.5", "invalid"):
            with self.subTest(limit=value):
                response = await client.get("/api/paper/trades", params={"limit": value})
                self.assertEqual(response.status, 400)
        self.assertEqual(journal.load(), (state, 1))

    async def test_disabled_paper_has_no_fabricated_history(self):
        client = await self.enterAsyncContext(TestClient(TestServer(create_app(run_feeds=False))))
        response = await client.get("/api/paper/trades")
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.json(), {"mode": "paper", "events": []})


if __name__ == "__main__":
    unittest.main()

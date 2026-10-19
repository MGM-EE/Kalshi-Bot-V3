"""Synthetic research evidence, off-thread persistence, and official labels."""
import asyncio
import json
import sqlite3
import tempfile
import threading
import time
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from feedboard.core import Settings, Store
from feedboard.paper_runtime import PaperJournal
from feedboard.research import ResearchDB, ResearchRecorder, SampleFeatures, iso
from feedboard.web import RESEARCH, STORE, create_app
from tests.test_paper_runtime import open_account, executable_exit

TICKER = "KXBTC15M-RESEARCH-TEST"


def market(**changes):
    return {"ticker": TICKER, "market_status": "open", "close_time": iso(time.time() + 600),
            "strike_price": 100000, "strike_source": "kalshi.floor_strike", **changes}


def record(sequence=1, **changes):
    return {"session_id": "session", "sequence": sequence, "kind": "snapshot",
            "captured_at": "2026-10-19T12:00:00.000+00:00", "ticker": TICKER,
            "payload": {"feeds": {"kalshi": {"data": {"markets": [market()]}}},
                        "book": market()}, **changes}


def rows(path):
    with sqlite3.connect(path) as connection:
        return [(kind, json.loads(zlib.decompress(payload))) for kind, payload in
                connection.execute("SELECT kind,payload FROM records ORDER BY sequence")]


class ResearchDBTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "research.sqlite3"
        self.db = ResearchDB(self.path)
        self.db.initialize("session", {"started_at": iso()})

    def test_idempotent_batches_preserve_finite_evidence_and_restart_sessions(self):
        sample = record()
        sample["payload"]["missing"] = float("nan")
        self.assertEqual(self.db.write([sample]), 1)
        self.assertEqual(self.db.write([sample]), 0)
        self.assertIsNone(rows(self.path)[0][1]["missing"])
        reopened = ResearchDB(self.path)
        reopened.initialize("next", {"started_at": iso()})
        self.assertEqual(len(rows(self.path)), 1)
        with reopened.connect() as connection:
            metadata = json.loads(connection.execute("SELECT metadata FROM sessions WHERE session_id='next'").fetchone()[0])
            self.assertEqual(len(metadata["source_sha256"]), 64)

    def test_unknown_results_stay_pending_and_only_finalized_labels_are_accepted(self):
        sample = record()
        sample["payload"]["book"] = market(close_time=iso(time.time() - 30))
        self.db.write([sample])
        self.assertEqual(self.db.due_market(time.time()), (TICKER, 0))
        self.db.outcome(TICKER, 0, market=market(market_status="closed", settlement_result="yes"))
        with self.db.connect() as connection:
            self.assertEqual(connection.execute("SELECT result,attempts FROM markets").fetchone(), (None, 1))
        self.db.outcome(TICKER, 1, market=market(market_status="finalized", settlement_result="no"),
                        raw={"status": "finalized", "result": "no"})
        self.assertIsNone(self.db.due_market(time.time() + 10000))
        with self.db.connect() as connection:
            row = connection.execute("SELECT result,result_at,metadata FROM markets").fetchone()
            self.assertEqual(row[0], "no")
            self.assertTrue(row[1])
            self.assertEqual(json.loads(row[2])["official_detail"]["result"], "no")

    def test_future_rounds_are_not_polled_and_transient_metadata_does_not_erase_floor(self):
        self.db.write([record()])
        sample = record(2)
        sample["payload"]["book"] = {"ticker": TICKER, "strike_price": None, "strike_source": None}
        sample["payload"]["feeds"] = {}
        self.db.write([sample])
        self.assertIsNone(self.db.due_market(time.time()))
        with self.db.connect() as connection:
            meta = json.loads(connection.execute("SELECT metadata FROM markets").fetchone()[0])
            self.assertEqual(meta["strike_price"], 100000)

    def test_research_refuses_existing_paper_or_corrupt_files_without_replacing_them(self):
        paper = self.path.parent / "paper.sqlite3"
        state = open_account().dump_state()
        PaperJournal(paper).save(state, state["events"], 0)
        with self.assertRaises(ValueError):
            ResearchDB(paper).initialize("wrong", {"started_at": iso()})
        self.assertEqual(PaperJournal(paper).load(), (state, 1))
        bad = self.path.parent / "bad.sqlite3"
        original = b"Do not replace an invalid existing research journal."
        bad.write_bytes(original)
        with self.assertRaises(sqlite3.Error):
            ResearchDB(bad).initialize("bad", {"started_at": iso()})
        self.assertEqual(bad.read_bytes(), original)

    def test_storage_budget_stops_new_samples_without_deleting_existing_evidence(self):
        self.db.write([record()])
        limited = ResearchDB(self.path, max_bytes=1)
        with self.assertRaisesRegex(OSError, "size limit"):
            limited.write([record(2)])
        self.assertEqual(len(rows(self.path)), 1)


class ResearchFeatureTests(unittest.TestCase):
    def test_directional_efficiency_and_preferred_entry_window_are_observations_only(self):
        features = SampleFeatures()
        wall = time.time()
        depth = market(close_time=iso(wall + 600))
        for second in range(62):
            mid = 100000 + second
            snapshot = {"feeds": {"kraken": {"status": "live", "age_ms": 0,
                "transport": "websocket", "updates": second + 1,
                "data": {"symbol": "BTC/USD", "bid": mid - .5, "ask": mid + .5}}}}
            result = features.snapshot(snapshot, depth, second, wall + second)
        self.assertAlmostEqual(result["efficiency_60s"], 1)
        self.assertGreater(result["momentum_60s_bps"], 0)
        self.assertTrue(result["within_preferred_5_to_13_minutes"])
        snapshot["feeds"]["kraken"]["age_ms"] = 3001
        result = features.snapshot(snapshot, depth, 62, wall + 62)
        self.assertFalse(result["quote_fresh"])
        self.assertIsNone(result["momentum_60s_bps"])


class ResearchAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "research.sqlite3"

    async def until(self, predicate):
        async with asyncio.timeout(6):
            while not predicate():
                await asyncio.sleep(.01)

    async def test_collection_needs_no_browser_and_flushes_on_shutdown(self):
        app = create_app(Settings(paper_enabled=True), run_feeds=False, research_path=self.path)
        async with TestClient(TestServer(app)) as client:
            recorder = app[RESEARCH]
            await self.until(lambda: recorder.written >= 2)
            response = await client.get("/api/snapshot")
            status = (await response.json())["research"]
            self.assertEqual(status["status"], "recording")
            self.assertFalse(app[STORE].paper["enabled"])
        data = rows(self.path)
        self.assertGreaterEqual(len(data), 2)
        self.assertEqual(data[0][0], "snapshot")
        self.assertIn("settlement_outlook", data[0][1])
        self.assertFalse(data[0][1]["coverage"]["live_settlement_index"])
        with ResearchDB(self.path).connect() as connection:
            ended, summary = connection.execute("SELECT ended_at,summary FROM sessions").fetchone()
            self.assertTrue(ended)
            self.assertEqual(json.loads(summary)["records_written"], len(data))

    async def test_slow_disk_does_not_block_quotes_and_queue_gaps_are_reported(self):
        store = Store(Settings())
        recorder = ResearchRecorder(store, self.path, interval=.01, queue_limit=2)
        store.research = recorder
        loop, started, release = asyncio.get_running_loop(), asyncio.Event(), threading.Event()
        real_write = recorder.database.write
        writer_threads = []

        def delayed_write(batch):
            writer_threads.append(threading.get_ident())
            loop.call_soon_threadsafe(started.set)
            if not release.wait(5):
                raise TimeoutError("Synthetic recorder disk was not released")
            return real_write(batch)

        with patch.object(recorder.database, "write", side_effect=delayed_write):
            await recorder.start()
            try:
                await asyncio.wait_for(started.wait(), 5)
                store.update("kraken", {"symbol": "BTC/USD", "last": 123}, "websocket")
                self.assertEqual(store.snapshot()["feeds"]["kraken"]["data"]["last"], 123)
                await self.until(lambda: recorder.dropped > 0)
                self.assertNotEqual(writer_threads[0], threading.get_ident())
                release.set()
                await self.until(lambda: recorder.written > 0)
            finally:
                release.set()
                await recorder.close()
        with recorder.database.connect() as connection:
            summary = json.loads(connection.execute("SELECT summary FROM sessions").fetchone()[0])
            self.assertGreater(summary["dropped_records"], 0)

    async def test_uncertain_commit_retries_the_same_batch_without_duplicate_evidence(self):
        recorder = ResearchRecorder(Store(Settings()), self.path, interval=.05)
        real_write, attempts = recorder.database.write, []

        def uncertain_commit(batch):
            attempts.append(tuple(row["sequence"] for row in batch))
            result = real_write(batch)
            if len(attempts) == 1:
                raise sqlite3.OperationalError("Synthetic lost commit acknowledgment")
            return result

        with patch.object(recorder.database, "write", side_effect=uncertain_commit), \
                self.assertLogs("feedboard.research", level="ERROR"):
            await recorder.start()
            try:
                await self.until(lambda: recorder.written > 0)
            finally:
                await recorder.close()
        self.assertEqual(attempts[0], attempts[1])
        with recorder.database.connect() as connection:
            count, unique = connection.execute("SELECT count(*),count(DISTINCT sequence) FROM records").fetchone()
            self.assertEqual(count, unique)
            self.assertEqual(count, recorder.written)

    async def test_committed_fill_records_exact_context_and_does_not_change_paper_account(self):
        paper = self.path.parent / "paper.sqlite3"
        original = open_account().dump_state()
        PaperJournal(paper).save(original, original["events"], 0)
        app = create_app(run_feeds=False, paper_path=paper, research_path=self.path)
        executable_exit(app[STORE])
        recorder = app[RESEARCH]
        observations = []
        real_record = recorder.record_fill

        def observe(engine, events):
            observations.append(PaperJournal(paper).load()[0]["event_seq"])
            real_record(engine, events)

        with patch.object(recorder, "record_fill", side_effect=observe):
            async with TestClient(TestServer(app)):
                await self.until(lambda: app[STORE].paper.get("last_event", {}).get("id") == 2)
                await self.until(lambda: any(kind == "paper_fill" for kind, _ in rows(self.path)))
        self.assertEqual(observations, [2])
        fill = next(value for kind, value in rows(self.path) if kind == "paper_fill")
        self.assertEqual(fill["event"]["reason"], "hard_95c")
        self.assertEqual(fill["decision_inputs"]["book"]["yes"][0][0], .95)
        self.assertTrue(fill["recorded_after_paper_commit"])
        self.assertEqual(PaperJournal(paper).load()[0]["event_seq"], 2)

    async def test_official_outcome_is_collected_for_an_untraded_round(self):
        calls = []

        async def detail(request):
            calls.append(request.match_info["ticker"])
            return web.json_response({"market": {"ticker": TICKER, "status": "finalized", "result": "yes",
                "close_time": iso(time.time() - 30), "market_type": "binary", "strike_type": "greater",
                "floor_strike": 100000, "rules_primary": "Synthetic official settlement rules"}})

        upstream = web.Application()
        upstream.router.add_get("/markets/{ticker}", detail)
        async with TestServer(upstream) as server:
            store = Store(Settings(kalshi_url=str(server.make_url("/markets"))))
            # A round seen in an earlier run must still be resolved after restart,
            # even if it never traded and no longer appears in the live listing.
            previous = ResearchDB(self.path)
            previous.initialize("previous-run", {"started_at": iso()})
            sample = record()
            sample["payload"]["book"] = market(close_time=iso(time.time() - 30))
            previous.write([sample])
            recorder = ResearchRecorder(store, self.path, interval=.02, outcome_interval=.02)
            await recorder.start()
            try:
                await self.until(lambda: bool(calls))
                await self.until(lambda: recorder.database.due_market(time.time() + 1000) is None)
            finally:
                await recorder.close()
        with recorder.database.connect() as connection:
            result, metadata = connection.execute("SELECT result,metadata FROM markets").fetchone()
            self.assertEqual(result, "yes")
            self.assertIn("rules_primary", json.loads(metadata)["official_detail"])
        self.assertEqual(store.paper, {"enabled": False, "mode": "paper", "status": "paused", "message": "Paper trading is disabled"})

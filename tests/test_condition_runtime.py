"""Conditions are live diagnostics, never account changes or uncommitted fills."""
import asyncio
import copy
import tempfile
import unittest
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from feedboard.paper import PaperConfig
from feedboard.paper_runtime import PaperJournal, paused_snapshot
from feedboard.web import STORE, create_app


class PausedConditionTests(unittest.TestCase):
    def test_journal_failure_invalidates_readiness_without_mutating_published_state(self):
        previous = {"position": {"side": "up"}, "cash": 900, "conditions": {
            "mode": "exit", "side": "up", "updated_at": 123,
            "summary": "Target reached", "items": [
                {"key": "profit", "label": "Profit target", "state": "met", "detail": "95c"},
                {"key": "stop", "label": "Stop loss", "state": "disabled", "detail": "Off"}]}}
        original = copy.deepcopy(previous)
        result = paused_snapshot("Journal unavailable", previous)
        self.assertEqual(previous, original)
        self.assertEqual(result["cash"], 900)
        self.assertEqual(result["conditions"]["mode"], "exit")
        self.assertEqual(result["conditions"]["side"], "up")
        self.assertIsNone(result["conditions"]["updated_at"])
        self.assertEqual([item["state"] for item in result["conditions"]["items"]],
                         ["building", "disabled"])


class ConditionHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_conditions_stream_without_writing_account_or_creating_trades(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "paper.sqlite3"
        app = create_app(run_feeds=False, paper_path=path,
                         paper_config=PaperConfig(entries_enabled=False))
        async with TestClient(TestServer(app)) as client:
            async with asyncio.timeout(5):
                while app[STORE].paper.get("conditions", {}).get("updated_at") is None:
                    await asyncio.sleep(.01)
            before = PaperJournal(path).load()
            before_revision = app[STORE].revision
            before_check = app[STORE].paper["conditions"]["updated_at"]
            response = await client.get("/api/snapshot")
            self.assertEqual(response.status, 200)
            document = await response.json()
            conditions = document["paper"]["conditions"]
            self.assertEqual(conditions["mode"], "entry")
            self.assertTrue(conditions["items"])
            self.assertTrue(all(item["state"] in {"met", "building", "blocked", "disabled"}
                                for item in conditions["items"]))
            async with client.get("/api/events") as stream:
                async with asyncio.timeout(5):
                    while True:
                        line = await stream.content.readline()
                        if line.startswith(b"data:"):
                            self.assertIn(b'"conditions":', line)
                            break
            await asyncio.sleep(.25)
            self.assertGreater(app[STORE].paper["conditions"]["updated_at"], before_check)
            self.assertEqual(app[STORE].revision, before_revision,
                             "Idle check timestamps must not force extra stream updates")
            self.assertEqual(PaperJournal(path).load(), before)
            self.assertNotIn("conditions", before[0])
            self.assertEqual(PaperJournal(path).recent_events(), [])
            self.assertIsNone(app[STORE].paper["position"])

    async def test_disabled_paper_does_not_publish_fake_readiness(self):
        async with TestClient(TestServer(create_app(run_feeds=False))) as client:
            response = await client.get("/api/snapshot")
            paper = (await response.json())["paper"]
            self.assertFalse(paper["enabled"])
            self.assertFalse(paper.get("conditions", {}).get("items"))

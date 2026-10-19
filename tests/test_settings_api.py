"""Offline settings HTTP integration with a real temporary SQLite journal."""

import asyncio
import copy
import sqlite3
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from feedboard.paper import PaperConfig, PaperEngine
from feedboard.paper_runtime import PaperJournal
from feedboard.strategy_settings import public_settings
from feedboard.web import JOURNAL, STORE, create_app


PATH = "/api/paper/settings"


def seeded_position():
    """Create a genuine entry using explicitly synthetic, fresh market input."""
    engine = PaperEngine()
    epoch = time.time()
    close = datetime.fromtimestamp(epoch + 900, timezone.utc).isoformat()
    for second in range(21):
        price = 65000 + second * 4
        feeds = {
            "kalshi": {"status": "live", "age_ms": 0},
            "kraken": {"status": "live", "age_ms": 0, "transport": "websocket",
                       "updates": second + 1, "data": {
                           "symbol": "BTC/USD", "bid": price - .5, "ask": price + .5,
                           "flow": {"buy_usd": 9000, "sell_usd": 1000,
                                    "large_buy_usd": 2000, "large_sell_usd": 0,
                                    "count": 20, "age_ms": 0, "window_seconds": 20}}},
        }
        book = {"ticker": "KXBTC15M-SETTINGS-TEST", "status": "live", "age_ms": 0,
                "version": second + 1, "yes": [[.48, 20]], "no": [[.50, 20]],
                "close_time": close, "market_status": "open",
                "strike_price": 66000, "strike_source": "kalshi.floor_strike"}
        engine.step(feeds, book, now=second, wall_time=epoch + second)
    if engine.position is None:
        raise AssertionError("Synthetic settings fixture did not enter")
    return engine.dump_state()


class SettingsApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "paper.sqlite3"

    async def until(self, predicate):
        async with asyncio.timeout(5):
            while not predicate():
                await asyncio.sleep(.01)

    async def client_for(self, *, journal=None, enabled=True):
        app = create_app(run_feeds=False, paper_path=self.path if enabled else None)
        if journal is not None:
            app[JOURNAL] = journal
        client = await self.enterAsyncContext(TestClient(TestServer(app)))
        if enabled:
            await self.until(lambda: app[STORE].paper_control.available)
        return client, app

    async def get_document(self, client):
        response = await client.get(PATH)
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        return await response.json()

    async def save(self, client, settings, revision=0, **kwargs):
        return await client.put(PATH, json={"settings": settings, "revision": revision}, **kwargs)

    async def test_defaults_are_readable_even_when_paper_is_unavailable(self):
        client, _ = await self.client_for(enabled=False)
        document = await self.get_document(client)
        self.assertEqual(document, {"settings": public_settings(PaperConfig()),
                                    "revision": 0, "available": False})
        self.assertTrue(document["settings"]["toward_strike_only"])
        response = await self.save(client, {"contracts": 2})
        self.assertEqual(response.status, 503)
        self.assertFalse(self.path.exists())

    async def test_complete_and_partial_saves_are_durable_and_do_not_create_trade_events(self):
        client, app = await self.client_for()
        document = await self.get_document(client)
        self.assertTrue(document["available"])
        full = {**document["settings"], "min_buy_cents": 12, "max_buy_cents": 65,
                "min_sell_cents": 82, "dynamic_min_cents": 84, "dynamic_max_cents": 94,
                "contracts": 2, "cooldown_seconds": 15}
        response = await self.save(client, full)
        self.assertEqual(response.status, 200)
        saved = await response.json()
        self.assertEqual(saved, {"status": "saved", "settings": full,
                                 "revision": 1, "available": True})
        persisted, disk_revision = PaperJournal(self.path).load()
        self.assertEqual(persisted["config_revision"], 1)
        self.assertEqual(persisted["config"]["min_entry_price"], .12)
        self.assertEqual(disk_revision, 2)
        response = await self.save(client, {"contracts": 3}, revision=1)
        self.assertEqual(response.status, 200)
        saved = await response.json()
        self.assertEqual(saved["settings"], {**full, "contracts": 3})
        self.assertEqual(saved["revision"], 2)
        self.assertEqual(app[STORE].paper_engine.config.contracts, 3)
        persisted, disk_revision = PaperJournal(self.path).load()
        self.assertEqual(persisted["config_revision"], 2)
        self.assertEqual(disk_revision, 3)
        self.assertEqual(persisted["cash"], 1000)
        self.assertEqual(persisted["events"], [])
        self.assertEqual(PaperJournal(self.path).recent_events(), [])
        response = await client.get("/api/paper/trades")
        self.assertEqual(await response.json(), {"mode": "paper", "events": []})

    async def test_response_waits_for_commit_and_get_keeps_last_committed_rules(self):
        loop = asyncio.get_running_loop()
        saving = asyncio.Event()
        release = threading.Event()
        writes = []

        class BlockingJournal(PaperJournal):
            def save(self, state, events, revision):
                writes.append(copy.deepcopy((state, events)))
                if state["config_revision"] == 1:
                    loop.call_soon_threadsafe(saving.set)
                    if not release.wait(5):
                        raise TimeoutError("Test did not release settings commit")
                return super().save(state, events, revision)

        client, app = await self.client_for(journal=BlockingJournal(self.path))
        original = PaperJournal(self.path).load()
        request = asyncio.create_task(self.save(client, {"contracts": 7}))
        try:
            await asyncio.wait_for(saving.wait(), 5)
            self.assertFalse(request.done())
            self.assertEqual(PaperJournal(self.path).load(), original)
            self.assertEqual((await self.get_document(client))["settings"]["contracts"], 5)
            self.assertEqual(app[STORE].paper_engine.config.contracts, 5)
            release.set()
            response = await asyncio.wait_for(request, 5)
            self.assertEqual(response.status, 200)
            self.assertEqual((await response.json())["settings"]["contracts"], 7)
            self.assertEqual(PaperJournal(self.path).load()[0]["config"]["contracts"], 7)
            self.assertTrue(all(events == [] for _, events in writes))
        finally:
            release.set()
            await asyncio.gather(request, return_exceptions=True)

    async def test_settings_survive_restart_without_resetting_position_cash_or_history(self):
        original = seeded_position()
        journal = PaperJournal(self.path)
        journal.save(original, original["events"], 0)
        client, app = await self.client_for()
        response = await self.save(client, {"contracts": 9, "stop_loss_cents": 6})
        self.assertEqual(response.status, 200)
        committed = PaperJournal(self.path).load()
        self.assertEqual(committed[0]["position"], original["position"])
        self.assertEqual(committed[0]["cash"], original["cash"])
        self.assertEqual(committed[0]["events"], original["events"])
        self.assertEqual(journal.recent_events(), original["events"][::-1])
        self.assertEqual(app[STORE].paper_engine.config.contracts, 9)
        self.assertEqual(app[STORE].paper_engine._position_config().contracts, 5)
        self.assertEqual(app[STORE].paper_engine._position_config().stop_loss, .12)
        await client.close()
        reopened, reopened_app = await self.client_for()
        document = await self.get_document(reopened)
        self.assertEqual(document["revision"], 1)
        self.assertEqual(document["settings"]["contracts"], 9)
        self.assertEqual(document["settings"]["stop_loss_cents"], 6)
        restored = reopened_app[STORE].paper_engine
        self.assertEqual(restored.position, original["position"])
        self.assertEqual(restored.cash, original["cash"])
        self.assertEqual(list(restored.events), original["events"])
        self.assertEqual(restored._position_config().stop_loss, .12)
        self.assertEqual(PaperJournal(self.path).load(), committed)
        # A detached simulation verifies behavior without racing the runtime owner.
        simulated = PaperEngine(state=restored.dump_state())
        epoch = simulated.position["opened_at"] + 1
        held_book = {"ticker": simulated.position["ticker"], "status": "live", "age_ms": 0,
                     "version": 100, "yes": [[.43, 20]], "no": [[.55, 20]],
                     "market_status": "open"}
        # A 7-cent decline is beyond the new 6-cent stop, but within the entry-time 12-cent stop.
        self.assertEqual(simulated.step({}, held_book, now=0, wall_time=epoch), [])
        held_book["yes"] = [[.37, 20]]
        held_book["no"] = [[.61, 20]]
        exited = simulated.step({}, held_book, now=1, wall_time=epoch + 1)
        self.assertEqual(exited[0]["reason"], "stop_loss")
        self.assertIsNone(simulated.position)
        epoch += 23
        close = datetime.fromtimestamp(epoch + 900, timezone.utc).isoformat()
        for second in range(21):
            price = 65000 + second * 4
            feeds = {
                "kalshi": {"status": "live", "age_ms": 0},
                "kraken": {"status": "live", "age_ms": 0, "transport": "websocket",
                           "updates": second + 1, "data": {
                               "symbol": "BTC/USD", "bid": price - .5, "ask": price + .5,
                               "flow": {"buy_usd": 9000, "sell_usd": 1000,
                                        "large_buy_usd": 2000, "large_sell_usd": 0,
                                        "count": 20, "age_ms": 0, "window_seconds": 20}}},
            }
            book = {**held_book, "version": 101 + second, "yes": [[.48, 20]],
                    "no": [[.50, 20]], "close_time": close,
                    "strike_price": 66000, "strike_source": "kalshi.floor_strike"}
            simulated.step(feeds, book, now=2 + second, wall_time=epoch + second)
        self.assertIsNotNone(simulated.position)
        self.assertEqual(simulated.position["contracts"], 9)
        self.assertEqual(simulated.position["rules"]["stop_loss"], .06)
        self.assertEqual(simulated.position["config_revision"], 1)

    async def test_invalid_settings_are_atomic_and_leave_account_and_revision_unchanged(self):
        client, _ = await self.client_for()
        before = PaperJournal(self.path).load()
        invalid = [None, [], {}, {"unknown": 2}, {"contracts": True},
                   {"contracts": 2.5}, {"contracts": "3"}, {"contracts": 0},
                   {"stop_loss_cents": float("nan")}, {"min_buy_cents": float("inf")},
                   {"dynamic_enabled": 1}, {"toward_strike_only": "true"},
                   {"min_buy_cents": 76}, {"max_buy_cents": 80},
                   {"dynamic_min_cents": 94, "dynamic_max_cents": 90},
                   {"dynamic_min_cents": 79}, {"dynamic_max_cents": 96},
                   {"contracts": 2, "stop_loss_cents": -1}]
        for values in invalid:
            with self.subTest(values=values):
                response = await self.save(client, values)
                self.assertEqual(response.status, 400, await response.text())
                self.assertEqual(PaperJournal(self.path).load(), before)
                self.assertEqual((await self.get_document(client))["revision"], 0)
        self.assertEqual(PaperJournal(self.path).recent_events(), [])

    async def test_bad_payloads_and_revisions_are_rejected(self):
        client, _ = await self.client_for()
        before = PaperJournal(self.path).load()
        payloads = [[], {}, {"settings": {"contracts": 2}},
                    {"settings": {"contracts": 2}, "revision": 0, "extra": True}]
        payloads += [{"settings": {"contracts": 2}, "revision": value}
                     for value in (None, True, -1, 1.5, "0")]
        for payload in payloads:
            with self.subTest(payload=payload):
                response = await client.put(PATH, json=payload)
                self.assertEqual(response.status, 400)
                self.assertEqual(PaperJournal(self.path).load(), before)
        response = await client.put(PATH, data="{broken", headers={"Content-Type": "application/json"})
        self.assertEqual(response.status, 400)

    async def test_stale_tab_revision_cannot_overwrite_newer_settings(self):
        client, _ = await self.client_for()
        tab_one = await self.get_document(client)
        tab_two = await self.get_document(client)
        response = await self.save(client, {"contracts": 2}, tab_one["revision"])
        self.assertEqual(response.status, 200)
        committed = PaperJournal(self.path).load()
        response = await self.save(client, {"contracts": 9}, tab_two["revision"])
        self.assertEqual(response.status, 409)
        self.assertIn("another tab", (await response.json())["error"])
        self.assertEqual(PaperJournal(self.path).load(), committed)
        self.assertEqual((await self.get_document(client))["settings"]["contracts"], 2)

    async def test_no_change_save_does_not_increment_either_revision(self):
        client, _ = await self.client_for()
        before = PaperJournal(self.path).load()
        response = await self.save(client, {"contracts": 5})
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())["revision"], 0)
        self.assertEqual(PaperJournal(self.path).load(), before)

    async def test_same_origin_json_is_required_for_changes(self):
        client, _ = await self.client_for()
        before = PaperJournal(self.path).load()
        origin = str(client.make_url("/")).rstrip("/")
        response = await client.put(PATH, data='{"settings":{"contracts":2},"revision":0}',
                                    headers={"Origin": origin, "Content-Type": "text/plain"})
        self.assertEqual(response.status, 415)
        for bad_origin in ("https://example.com", "null", origin.replace("http:", "https:"),
                           origin + "/path", origin + "?query=1", origin + "#fragment",
                           origin.replace("://", "://user@")):
            with self.subTest(origin=bad_origin):
                response = await self.save(client, {"contracts": 2}, headers={"Origin": bad_origin})
                self.assertEqual(response.status, 403)
                self.assertEqual(PaperJournal(self.path).load(), before)
        response = await self.save(client, {"contracts": 2}, headers={"Origin": origin})
        self.assertEqual(response.status, 200)

    async def test_settings_journal_failure_pauses_without_claiming_saved_or_losing_data(self):
        class FailingJournal(PaperJournal):
            def save(self, state, events, revision):
                if state["config_revision"]:
                    raise sqlite3.OperationalError("Synthetic settings disk failure")
                return super().save(state, events, revision)

        client, app = await self.client_for(journal=FailingJournal(self.path))
        before = PaperJournal(self.path).load()
        with self.assertLogs("feedboard.paper_runtime", level="ERROR"):
            response = await self.save(client, {"contracts": 2})
        self.assertEqual(response.status, 503)
        result = await response.json()
        self.assertNotIn("status", result)
        self.assertIn("could not be confirmed", result["error"])
        self.assertEqual(PaperJournal(self.path).load(), before)
        document = await self.get_document(client)
        self.assertFalse(document["available"])
        self.assertEqual(document["settings"]["contracts"], 5)
        self.assertEqual(document["revision"], 0)
        self.assertEqual(app[STORE].paper["status"], "paused")
        response = await self.save(client, {"contracts": 3})
        self.assertEqual(response.status, 503)
        self.assertEqual(PaperJournal(self.path).recent_events(), [])


if __name__ == "__main__":
    unittest.main()

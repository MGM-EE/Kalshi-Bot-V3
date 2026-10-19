"""Settlement outlook follows real Store quote ingestion, not browser polling."""
import asyncio
import copy
import json
import math
import time
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from aiohttp.test_utils import TestClient, TestServer

from feedboard.core import Settings, Store
from feedboard.paper import PaperEngine
from feedboard.web import STORE, create_app


def warmed_store():
    store = Store(Settings(paper_enabled=True))
    wall, mono = time.time() - 65, time.monotonic() - 65
    for second in range(66):
        mid = 100010 + math.sin(second / 5) * 5
        with patch("feedboard.core.time.monotonic", return_value=mono + second), \
                patch("feedboard.core.time.time", return_value=wall + second):
            store.update("kraken", {"symbol": "BTC/USD", "bid": mid - .5,
                                    "ask": mid + .5, "last": mid}, "websocket")
    store.book = {"ticker": "KXBTC15M-OUTLOOK-INTEGRATION", "status": "live", "version": 1,
                  "market_status": "open", "strike_price": 100000,
                  "strike_source": "kalshi.floor_strike", "yes": [[.48, 20]], "no": [[.50, 20]],
                  "close_time": datetime.fromtimestamp(wall + 900, timezone.utc).isoformat(),
                  "settlement_result": None}
    store.book_mono = mono + 65
    return store


class OutlookStoreTests(unittest.TestCase):
    def test_real_quote_ingestion_is_visible_and_repeated_reads_do_not_change_account(self):
        store = warmed_store()
        store.paper_engine = PaperEngine()
        state = store.paper_engine.dump_state()
        before = store.snapshot()["settlement_outlook"]
        revision = store.revision
        self.assertEqual(before["status"], "live")
        self.assertEqual(before["up_percent"] + before["down_percent"], 100)
        self.assertTrue(before["history"])
        for _ in range(20):
            current = store.snapshot()["settlement_outlook"]
            self.assertEqual(current["history"], before["history"])
            self.assertEqual(current["updated_at"], before["updated_at"])
            json.dumps(current, allow_nan=False)
        self.assertEqual(store.revision, revision)
        self.assertEqual(store.paper_engine.dump_state(), state)

    def test_book_disconnect_and_rest_fallback_hide_current_percentages(self):
        store = warmed_store()
        self.assertEqual(store.snapshot()["settlement_outlook"]["status"], "live")
        store.book["status"] = "retrying"
        disconnected = store.snapshot()["settlement_outlook"]
        self.assertIsNone(disconnected["up_percent"])
        store.book["status"] = "live"
        store.update("kraken", {"symbol": "BTC/USD", "bid": 99999, "ask": 100001}, "rest")
        self.assertIsNone(store.snapshot()["settlement_outlook"]["up_percent"])

    def test_disabled_and_other_feed_modes_cannot_publish_an_enabled_forecast(self):
        for settings in (Settings(), Settings(paper_enabled=True, series="OTHER"),
                         Settings(paper_enabled=True, symbol="ETH/USD")):
            with self.subTest(settings=settings):
                view = Store(settings).snapshot()["settlement_outlook"]
                self.assertFalse(view["enabled"])
                self.assertIsNone(view["up_percent"])


class OutlookHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_outlook_is_in_http_and_stream_without_enabling_paper_trades(self):
        app = create_app(Settings(paper_enabled=True), run_feeds=False)
        store = warmed_store()
        app[STORE] = store
        original = copy.deepcopy(store.paper)
        async with TestClient(TestServer(app)) as client:
            response = await client.get("/api/snapshot")
            self.assertEqual(response.status, 200)
            view = (await response.json())["settlement_outlook"]
            self.assertEqual(view["status"], "live")
            self.assertGreater(view["elapsed_seconds"], 60)
            self.assertLess(view["remaining_seconds"], 840)
            async with client.get("/api/events") as stream:
                async with asyncio.timeout(5):
                    while True:
                        line = await stream.content.readline()
                        if line.startswith(b"data:"):
                            streamed = json.loads(line[5:])["settlement_outlook"]
                            self.assertEqual(streamed["history"], view["history"])
                            break
            self.assertEqual(store.paper, original)
            self.assertIsNone(store.paper_engine)

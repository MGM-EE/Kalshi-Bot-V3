"""Offline HTTP/WS integration with explicitly synthetic local exchange servers."""

import asyncio
import json
import os
import unittest
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from feedboard.core import Settings
from feedboard.web import MAX_CLIENTS, STORE, create_app


def synthetic_markets(count=1):
    return {"markets": [{"ticker": f"TEST-{index}", "title": "Synthetic integration sample",
                         "yes_bid_dollars": "0.400000", "yes_ask_dollars": "0.420000",
                         "last_price_dollars": "0.410000"} for index in range(count)]}


def synthetic_ticker(last):
    return {"channel": "ticker", "type": "update", "data": [{
        "symbol": "BTC/USD", "bid": last - 0.5, "ask": last + 0.5, "last": last,
    }]}


class AsyncTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # These tests never contact exchanges, and local servers must not use an inherited proxy.
        proxy_bypass = patch.dict(os.environ, {"NO_PROXY": "127.0.0.1,localhost",
                                              "no_proxy": "127.0.0.1,localhost"})
        proxy_bypass.start()
        self.addCleanup(proxy_bypass.stop)

    async def client_for(self, app):
        return await self.enterAsyncContext(TestClient(TestServer(app)))

    async def until(self, predicate):
        # A generous completion deadline catches stalled tasks, not machine-speed regressions.
        async with asyncio.timeout(5):
            while not predicate():
                await asyncio.sleep(0.01)

    async def read_event(self, response):
        async with asyncio.timeout(5):
            frame = await response.content.readuntil(b"\n\n")
        self.assertTrue(frame.startswith(b"event: snapshot\n"))
        return json.loads(frame.split(b"data: ", 1)[1].strip())


class WebTests(AsyncTests):
    async def test_health_separates_running_from_feed_readiness(self):
        app = create_app(run_feeds=False)
        client = await self.client_for(app)
        response = await client.get("/healthz")
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())["ready"], False)
        response = await client.get("/readyz")
        self.assertEqual(response.status, 503)
        app[STORE].update("kalshi", {"series": "TEST", "markets": [{"ticker": "TEST-1"}]}, "rest")
        app[STORE].update("kraken", {"symbol": "BTC/USD", "last": 65000}, "websocket")
        response = await client.get("/readyz")
        self.assertEqual(response.status, 200)
        self.assertTrue((await response.json())["ready"])
        app[STORE].feeds["kalshi"].received_mono -= 30
        response = await client.get("/readyz")
        self.assertEqual(response.status, 503)
        self.assertEqual((await response.json())["feeds"]["kalshi"], "stale")
        response = await client.get("/api/snapshot")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual((await response.json())["feeds"]["kraken"]["data"]["last"], 65000)

    async def test_sse_coalesces_a_burst_to_the_newest_state(self):
        app = create_app(run_feeds=False)
        client = await self.client_for(app)
        store = app[STORE]
        response = await client.get("/api/events")
        self.assertEqual(response.headers["Content-Type"], "text/event-stream")
        initial = await self.read_event(response)
        self.assertIsNone(initial["feeds"]["kraken"]["data"]["last"])
        self.assertEqual(store.clients, 1)
        # No await in this burst: the next frame must describe its last value, without replaying quotes.
        for index in range(100):
            store.update("kraken", {"symbol": "BTC/USD", "last": 65000 + index}, "websocket")
        latest = await self.read_event(response)
        self.assertEqual(latest["feeds"]["kraken"]["data"]["last"], 65099)
        self.assertEqual(latest["feeds"]["kraken"]["updates"], 100)
        response.close()
        store.update("kraken", {"symbol": "BTC/USD", "last": 65100}, "websocket")
        await self.until(lambda: store.clients == 0)

    async def test_dashboard_client_limit_is_enforced(self):
        app = create_app(run_feeds=False)
        client = await self.client_for(app)
        app[STORE].clients = MAX_CLIENTS
        response = await client.get("/api/events")
        self.assertEqual(response.status, 503)
        self.assertIn("Too many dashboard clients", await response.text())


class ExchangeIntegrationTests(AsyncTests):
    async def test_slow_and_failing_kalshi_does_not_block_kraken_or_erase_quotes(self):
        release_kalshi = asyncio.Event()
        kalshi_started = asyncio.Event()
        fail_kalshi = asyncio.Event()
        subscriptions = asyncio.Queue()
        socket = asyncio.get_running_loop().create_future()
        queries = []

        async def kalshi_handler(request):
            queries.append(dict(request.query))
            kalshi_started.set()
            await release_kalshi.wait()
            if fail_kalshi.is_set():
                raise web.HTTPServiceUnavailable(text="Synthetic upstream outage")
            return web.json_response(synthetic_markets(8))

        async def ws_handler(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            await subscriptions.put(await ws.receive_json())
            await ws.send_json(synthetic_ticker(65000))
            if not socket.done():
                socket.set_result(ws)
            async for _ in ws:
                pass
            return ws

        upstream = web.Application()
        upstream.router.add_get("/kalshi", kalshi_handler)
        upstream.router.add_get("/kraken", ws_handler)
        server = await self.enterAsyncContext(TestServer(upstream))
        app = create_app(Settings(kalshi_url=str(server.make_url("/kalshi")),
                                  kraken_ws_url=str(server.make_url("/kraken")),
                                  kraken_rest_url=str(server.make_url("/unused-rest")),
                                  poll_seconds=0.25, request_timeout=3,
                                  retry_base=0.05, retry_cap=0.05))
        client = await self.client_for(app)
        store = app[STORE]
        self.addCleanup(release_kalshi.set)
        await self.until(lambda: kalshi_started.is_set() and store.feeds["kraken"].updates > 0)
        subscription = await asyncio.wait_for(subscriptions.get(), 5)
        self.assertEqual(subscription["method"], "subscribe")
        self.assertEqual(subscription["params"]["symbol"], ["BTC/USD"])
        self.assertEqual(subscription["params"]["channel"], "ticker")
        self.assertEqual(subscription["params"]["event_trigger"], "bbo")
        response = await client.get("/api/snapshot")
        snapshot = await response.json()
        self.assertEqual(snapshot["feeds"]["kraken"]["status"], "live")
        self.assertEqual(snapshot["feeds"]["kalshi"]["status"], "connecting")
        self.assertFalse(store.ready())
        release_kalshi.set()
        await self.until(store.ready)
        self.assertEqual(queries[0], {"series_ticker": "KXBTC15M", "status": "open", "limit": "3"})
        retained = store.snapshot()["feeds"]["kalshi"]["data"]
        self.assertEqual(len(retained["markets"]), 3)
        self.assertEqual(retained["markets"][0]["yes_bid"], 0.4)
        fail_kalshi.set()
        with self.assertLogs("feedboard.core", level="WARNING"):
            await self.until(lambda: store.feeds["kalshi"].status == "retrying")
        ws = await socket
        await ws.send_json(synthetic_ticker(65010))
        await self.until(lambda: store.feeds["kraken"].data["last"] == 65010)
        snapshot = store.snapshot()
        self.assertEqual(snapshot["feeds"]["kalshi"]["data"], retained)
        self.assertEqual(snapshot["feeds"]["kraken"]["status"], "live")
        self.assertEqual((await client.get("/readyz")).status, 503)

    async def test_websocket_disconnect_uses_rest_sample_then_reconnects(self):
        connections = 0
        rest_queries = []
        rest_seen = asyncio.Event()

        async def kalshi_handler(request):
            return web.json_response(synthetic_markets())

        async def rest_handler(request):
            rest_queries.append(dict(request.query))
            rest_seen.set()
            return web.json_response({"error": [], "result": {"XXBTZUSD": {
                "b": ["64999"], "a": ["65001"], "c": ["65000"],
            }}})

        async def ws_handler(request):
            nonlocal connections
            connections += 1
            attempt = connections
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            await ws.receive_json()
            await ws.send_json(synthetic_ticker(65000 + attempt))
            if attempt == 1:
                await ws.close()
            else:
                async for _ in ws:
                    pass
            return ws

        upstream = web.Application()
        upstream.router.add_get("/kalshi", kalshi_handler)
        upstream.router.add_get("/kraken-rest", rest_handler)
        upstream.router.add_get("/kraken-ws", ws_handler)
        server = await self.enterAsyncContext(TestServer(upstream))
        app = create_app(Settings(kalshi_url=str(server.make_url("/kalshi")),
                                  kraken_rest_url=str(server.make_url("/kraken-rest")),
                                  kraken_ws_url=str(server.make_url("/kraken-ws")),
                                  poll_seconds=0.25, retry_base=0.05, retry_cap=0.05))
        with self.assertLogs("feedboard.core", level="WARNING"):
            await self.client_for(app)
            store = app[STORE]
            await self.until(lambda: rest_seen.is_set() and store.feeds["kraken"].transport == "rest")
        self.assertEqual(rest_queries, [{"pair": "BTC/USD"}])
        self.assertEqual(store.feeds["kraken"].data["last"], 65000)
        await self.until(lambda: store.feeds["kraken"].transport == "websocket"
                         and store.feeds["kraken"].updates >= 3)
        self.assertGreaterEqual(connections, 2)
        self.assertEqual(store.feeds["kraken"].data["last"], 65002)
        self.assertTrue(store.ready())


if __name__ == "__main__":
    unittest.main()

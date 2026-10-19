"""Paper feed wiring against explicitly synthetic local REST and WebSocket servers."""
import asyncio
import os
import time
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer

from feedboard.core import Feeds, Settings, Store, normalize_kalshi, start_feeds, stop_feeds


TICKER = "KXBTC15M-FEED-TEST"


def iso(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat()


def market(*, status="active", result=None, close_time=None):
    return {"ticker": TICKER, "title": "Synthetic local contract",
            "status": status, "result": result,
            "close_time": close_time or iso(time.time() + 900),
            "yes_bid_dollars": "0.4800", "yes_ask_dollars": "0.5000",
            "no_bid_dollars": "0.5000", "no_ask_dollars": "0.5200",
            "last_price_dollars": "0.4900"}


def depth():
    return {"orderbook_fp": {
        "yes_dollars": [["0.4700", "12.40"], ["0.4800", "2.75"]],
        "no_dollars": [["0.5000", "4.50"]],
    }}


class PaperFeedTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        bypass = patch.dict(os.environ, {"NO_PROXY": "127.0.0.1,localhost",
                                        "no_proxy": "127.0.0.1,localhost"})
        bypass.start()
        self.addCleanup(bypass.stop)

    async def until(self, predicate):
        async with asyncio.timeout(5):
            while not predicate():
                await asyncio.sleep(.01)

    async def stop_task(self, task):
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def direct_book_feed(self, app, *, listing_status="retrying", listing_age=30,
                               held_close=None):
        server = await self.enterAsyncContext(TestServer(app))
        settings = Settings(kalshi_url=str(server.make_url("/markets")),
                            paper_enabled=True, poll_seconds=.25)
        store = Store(settings)
        cached = normalize_kalshi({"markets": [market()]}, 1)
        store.update("kalshi", {"series": "KXBTC15M", "markets": cached}, "rest")
        store.feeds["kalshi"].status = listing_status
        store.feeds["kalshi"].received_mono -= listing_age
        position = {"ticker": TICKER, "side": "up", "close_time": held_close or iso(time.time() - 10)}
        store.paper_engine = SimpleNamespace(snapshot=lambda: {"position": position})
        session = await self.enterAsyncContext(aiohttp.ClientSession())
        task = asyncio.create_task(Feeds(store, session).orderbook())
        self.addAsyncCleanup(self.stop_task, task)
        return store, task

    async def test_paper_feeds_subscribe_to_live_trades_and_normalize_fractional_depth(self):
        subscriptions, market_queries, book_queries = [], [], []

        async def markets_handler(request):
            market_queries.append(dict(request.query))
            return web.json_response({"markets": [market()]})

        async def book_handler(request):
            book_queries.append((request.match_info["ticker"], dict(request.query)))
            return web.json_response(depth())

        async def ws_handler(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            subscriptions.append(await ws.receive_json())
            subscriptions.append(await ws.receive_json())
            await ws.send_json({"channel": "ticker", "type": "update", "data": [{
                "symbol": "BTC/USD", "bid": 64999, "ask": 65001, "last": 65000,
            }]})
            stamp = iso(time.time())
            await ws.send_json({"channel": "trade", "type": "update", "data": [
                {"symbol": "BTC/USD", "side": "buy", "price": 65000, "qty": 2,
                 "trade_id": 901, "ord_type": "market", "timestamp": stamp},
                {"symbol": "BTC/USD", "side": "sell", "price": 65000, "qty": .25,
                 "trade_id": 902, "ord_type": "limit", "timestamp": stamp},
                {"symbol": "BTC/USD", "side": "buy", "price": 65000, "qty": .1,
                 "trade_id": 903, "ord_type": "market", "timestamp": stamp},
            ]})
            await ws.send_json({"channel": "ticker", "type": "update", "data": [{
                "symbol": "BTC/USD", "bid": 65001, "ask": 65003, "last": 65002,
            }]})
            async for _ in ws:
                pass
            return ws

        upstream = web.Application()
        upstream.router.add_get("/markets", markets_handler)
        upstream.router.add_get("/markets/{ticker}/orderbook", book_handler)
        upstream.router.add_get("/kraken", ws_handler)
        server = await self.enterAsyncContext(TestServer(upstream))
        settings = Settings(kalshi_url=str(server.make_url("/markets")),
                            kraken_ws_url=str(server.make_url("/kraken")),
                            kraken_rest_url=str(server.make_url("/unused-rest")),
                            paper_enabled=True, poll_seconds=.25)
        store = Store(settings)
        session, tasks = await start_feeds(store)
        self.addAsyncCleanup(stop_feeds, session, tasks)
        await self.until(lambda: store.book["status"] == "live"
                         and store.feeds["kraken"].data.get("last") == 65002
                         and store.trade_flow.snapshot(time.monotonic())["count"] == 3)
        self.assertEqual(subscriptions, [
            {"method": "subscribe", "params": {"channel": "ticker", "symbol": ["BTC/USD"],
                                                 "event_trigger": "bbo", "snapshot": True}},
            {"method": "subscribe", "params": {"channel": "trade", "symbol": ["BTC/USD"],
                                                 "snapshot": False}},
        ])
        self.assertEqual(market_queries[0], {"series_ticker": "KXBTC15M", "status": "open", "limit": "3"})
        self.assertEqual(book_queries[0], (TICKER, {"depth": "5"}))
        flow = store.snapshot()["feeds"]["kraken"]["data"]["flow"]
        self.assertEqual(flow["count"], 3)
        self.assertEqual(flow["buy_usd"], 136500)
        self.assertEqual(flow["sell_usd"], 16250)
        self.assertEqual(flow["large_buy_usd"], 130000)
        self.assertEqual(flow["large_sell_usd"], 0)
        self.assertIsNotNone(flow["age_ms"])
        self.assertLessEqual(flow["age_ms"], 3000)
        book = store.book_snapshot()
        self.assertEqual(book["ticker"], TICKER)
        self.assertEqual(book["status"], "live")
        self.assertEqual(book["market_status"], "active")
        self.assertIsInstance(book["version"], int)
        self.assertGreater(book["version"], 0)
        self.assertEqual(book["yes"], [[.48, 2.75], [.47, 12.4]])
        self.assertEqual(book["no"], [[.50, 4.5]])
        self.assertIsNone(book["settlement_result"])
        self.assertTrue(store.ready())
        self.assertTrue(any(task.get_name() == "kalshi-book" for task in tasks))

    async def test_held_expired_market_refreshes_cached_listing_for_official_settlement(self):
        for listing_status, listing_age in (("retrying", 30), ("live", 30), ("live", 0)):
            with self.subTest(listing_status=listing_status, listing_age=listing_age):
                detail_requests, book_requests = [], []

                async def detail_handler(request):
                    detail_requests.append(request.match_info["ticker"])
                    return web.json_response({"market": market(status="finalized", result="yes",
                                                               close_time=iso(time.time() - 10))})

                async def book_handler(request):
                    book_requests.append(request.match_info["ticker"])
                    return web.json_response(depth())

                upstream = web.Application()
                upstream.router.add_get("/markets/{ticker}", detail_handler)
                upstream.router.add_get("/markets/{ticker}/orderbook", book_handler)
                store, task = await self.direct_book_feed(upstream, listing_status=listing_status,
                                                          listing_age=listing_age)
                await self.until(lambda: store.book.get("settlement_result") == "yes")
                self.assertEqual(detail_requests[0], TICKER)
                self.assertEqual(book_requests, [])
                self.assertEqual(store.book["market_status"], "finalized")
                self.assertEqual(store.book["status"], "live")
                self.assertEqual(store.book["yes"], [])
                self.assertEqual(store.book["no"], [])
                self.assertGreater(store.book["version"], 0)
                # The final result came from detail, despite the retained active listing row.
                self.assertEqual(store.feeds["kalshi"].data["markets"][0]["market_status"], "active")
                await self.stop_task(task)

    async def test_tentative_result_on_active_detail_is_not_a_settlement(self):
        detail_requests, book_requests = [], []

        async def detail_handler(request):
            detail_requests.append(request.match_info["ticker"])
            return web.json_response({"market": market(status="active", result="yes")})

        async def book_handler(request):
            book_requests.append(dict(request.query))
            return web.json_response(depth())

        upstream = web.Application()
        upstream.router.add_get("/markets/{ticker}", detail_handler)
        upstream.router.add_get("/markets/{ticker}/orderbook", book_handler)
        store, _ = await self.direct_book_feed(upstream, held_close=iso(time.time() + 900))
        await self.until(lambda: store.book["status"] == "live")
        self.assertEqual(detail_requests[0], TICKER)
        self.assertEqual(book_requests[0], {"depth": "5"})
        self.assertEqual(store.book["market_status"], "active")
        self.assertIsNone(store.book["settlement_result"])
        self.assertEqual(store.book["yes"], [[.48, 2.75], [.47, 12.4]])
        self.assertEqual(store.book["no"], [[.50, 4.5]])


if __name__ == "__main__":
    unittest.main()

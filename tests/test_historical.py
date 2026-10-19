"""Offline cache/normalization and local fake Kalshi live/archive API tests."""
import tempfile
import unittest
from pathlib import Path

from aiohttp import web
from aiohttp.test_utils import TestServer

from feedboard.historical import (DownloadError, HistoryCache, HistoryDownloader,
                                 bar_price, compile_market)
from feedboard.paper import PaperEngine
from feedboard.paper_runtime import PaperJournal

BASE = 1_780_000_000 // 900 * 900


def iso(stamp):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat()


def raw_market(index=1, **changes):
    return {"ticker": f"KXBTC15M-HISTORY-{index}", "close_time": iso(BASE + index * 900),
            "settlement_ts": iso(BASE + index * 900 + 1.8), "status": "finalized", "result": "yes",
            "market_type": "binary", "strike_type": "greater_or_equal", "floor_strike": 100000, **changes}


def candles(index=1, *, historical=False):
    return [{"end_period_ts": BASE + (index - 1) * 900 + (minute + 1) * 60,
             "yes_bid": {"open" if historical else "open_dollars": ".29"},
             "yes_ask": {"open" if historical else "open_dollars": ".30"},
             "price": {"high_dollars": ".99", "close_dollars": ".9"}}
            for minute in range(15)]


class HistoryNormalizationTests(unittest.TestCase):
    def test_fixed_point_archive_and_legacy_live_units_and_null_precedence(self):
        self.assertEqual(bar_price({"open_dollars": ".30", "open": 99}, False), .3)
        self.assertEqual(bar_price({"open": "0.30"}, True), .3)
        self.assertEqual(bar_price({"open": 30}, False), .3)
        for value in ({"open_dollars": None, "open": 30}, {"open_dollars": True},
                      {"open_dollars": "NaN"}, {"open_dollars": "1.01"}):
            self.assertIsNone(bar_price(value, False))

    def test_no_synthetic_missing_candles_or_future_high_low_information(self):
        rows = candles()[:5] + candles()[6:]
        first = compile_market(raw_market(), rows, historical=False)
        self.assertEqual(len(first.bars), 14)
        for row in rows:
            row["price"] = {"high_dollars": "0", "close_dollars": "0"}
            row["yes_bid"]["high_dollars"] = ".99"
        second = compile_market(raw_market(), rows, historical=False)
        self.assertEqual(first, second)
        self.assertEqual(first.settled, BASE + 902)

    def test_supported_official_settlement_and_verified_strike_are_required(self):
        for changes in ({"status": "closed"}, {"result": None}, {"floor_strike": None},
                        {"strike_type": "less"}, {"market_type": "scalar"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                compile_market(raw_market(**changes), candles(), historical=False)
        compiled = compile_market(raw_market(settlement_ts=None), candles(), historical=False)
        self.assertFalse(compiled.settlement_time_known)
        self.assertEqual(compiled.settled, compiled.close)

    def test_duplicate_conflicts_and_outside_round_candles(self):
        rows = candles()
        extra = {**rows[0], "end_period_ts": BASE}
        self.assertEqual(len(compile_market(raw_market(), [extra, *rows, rows[0]], historical=False).bars), 15)
        with self.assertRaises(ValueError):
            compile_market(raw_market(), [*rows, {**rows[0], "yes_bid": {"open_dollars": ".5"}}], historical=False)


class HistoryCacheTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "history.sqlite3"
        self.cache = HistoryCache(self.path)
        self.cache.initialize()

    def test_resume_partial_cache_and_stable_dataset_fingerprint(self):
        self.cache.catalog(BASE, BASE + 2700, [(raw_market(1), False), (raw_market(2), False)], {})
        self.cache.save_candles(raw_market(1), candles(1), False)
        with self.assertRaisesRegex(ValueError, "still need"):
            self.cache.load(BASE, BASE + 2700)
        markets, info = self.cache.load(BASE, BASE + 2700, allow_partial=True)
        self.assertEqual(info["missing_tickers"], [raw_market(2)["ticker"]])
        self.assertEqual(len(markets), 1)
        self.cache.save_candles(raw_market(2), candles(2), False)
        first, data = self.cache.load(BASE, BASE + 2700)
        second, again = self.cache.load(BASE, BASE + 2700)
        self.assertEqual(first, second)
        self.assertEqual(data["sha256"], again["sha256"])
        self.assertEqual(self.cache.latest_range(), (BASE, BASE + 2700))

    def test_existing_account_is_refused_and_preserved(self):
        path = self.path.parent / "paper.sqlite3"
        state = PaperEngine().dump_state()
        PaperJournal(path).save(state, [], 0)
        with self.assertRaises(ValueError):
            HistoryCache(path).initialize()
        self.assertEqual(PaperJournal(path).load(), (state, 1))


class HistoryDownloadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.cache = HistoryCache(Path(directory.name) / "history.sqlite3")
        self.cache.initialize()

    async def test_paginated_live_archive_batch_handoff_and_cache_hit(self):
        calls = []
        upstream = web.Application()

        async def cutoff(request):
            calls.append(request.path)
            return web.json_response({"market_settled_ts": iso(BASE + 1800)})

        async def listing(request):
            calls.append(request.path)
            self.assertEqual(request.query["min_close_ts"], str(BASE))
            self.assertEqual(request.query["max_close_ts"], str(BASE + 3600 - 1))
            return web.json_response({"markets": [raw_market(3)] if request.query.get("cursor") else [raw_market(2)],
                                      "cursor": "" if request.query.get("cursor") else "next"})

        async def archive(request):
            calls.append(request.path)
            self.assertNotIn("min_close_ts", request.query)
            return web.json_response({"markets": [raw_market(1), raw_market(2)], "cursor": ""})

        async def batch(request):
            calls.append(request.path)
            return web.json_response({"markets": [{"market_ticker": raw_market(2)["ticker"], "candlesticks": candles(2)}]})

        async def live_candle(request):
            calls.append(request.path)
            # A market may be archived between the catalog and candle request.
            return web.json_response({"candlesticks": []})

        async def old_candle(request):
            calls.append(request.path)
            index = int(request.match_info["ticker"].rsplit("-", 1)[-1])
            return web.json_response({"candlesticks": candles(index, historical=True)})

        upstream.router.add_get("/historical/cutoff", cutoff)
        upstream.router.add_get("/markets", listing)
        upstream.router.add_get("/historical/markets", archive)
        upstream.router.add_get("/markets/candlesticks", batch)
        upstream.router.add_get("/series/KXBTC15M/markets/{ticker}/candlesticks", live_candle)
        upstream.router.add_get("/historical/markets/{ticker}/candlesticks", old_candle)
        async with TestServer(upstream) as server:
            downloader = HistoryDownloader(self.cache, base_url=str(server.make_url("")).rstrip("/"),
                                           request_interval=0, progress=lambda _: None)
            await downloader.download(BASE, BASE + 3600)
            count = len(calls)
            await downloader.download(BASE, BASE + 3600)
            self.assertEqual(len(calls), count)
        markets, info = self.cache.load(BASE, BASE + 3600)
        self.assertEqual(len(markets), 3)
        self.assertEqual(info["missing_minutes"], 0)
        self.assertTrue(all(m.bars[0].yes_ask == .3 for m in markets))
        self.assertIn("/series/KXBTC15M/markets/KXBTC15M-HISTORY-3/candlesticks", calls)

    async def test_rate_limit_retry_and_repeated_pagination_are_not_silent(self):
        attempts = []
        upstream = web.Application()

        async def cutoff(request):
            attempts.append(1)
            if len(attempts) == 1:
                return web.Response(status=429, headers={"Retry-After": "0"})
            return web.json_response({"market_settled_ts": iso(BASE - 1)})

        async def listing(request):
            return web.json_response({"markets": [raw_market()], "cursor": "repeated"})

        upstream.router.add_get("/historical/cutoff", cutoff)
        upstream.router.add_get("/markets", listing)
        async with TestServer(upstream) as server:
            downloader = HistoryDownloader(self.cache, base_url=str(server.make_url("")).rstrip("/"),
                                           request_interval=0, progress=lambda _: None)
            with self.assertRaisesRegex(DownloadError, "pagination"):
                await downloader.download(BASE, BASE + 1800)
        self.assertEqual(len(attempts), 2)
        self.assertIsNone(self.cache.manifest(BASE, BASE + 1800))

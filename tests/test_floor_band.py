"""Synthetic floor-distance scenarios and local persistent settings checks."""
import asyncio
import copy
import tempfile
import unittest
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from feedboard.core import Settings
from feedboard.paper import FLOOR_BAND_FIELDS, PaperConfig, PaperEngine
from feedboard.web import STORE, create_app
from tests.test_paper import EPOCH, book, feeds


def entry(mid, *, side="up", strike=100000, source="kalshi.floor_strike", **settings):
    config = PaperConfig(require_toward_strike=False, floor_distance_enabled=True, **settings)
    engine = PaperEngine(config)
    direction = 1 if side == "up" else -1
    events = []
    for second in range(21):
        price = mid - direction * (20 - second) * 4
        events.extend(engine.step(feeds(price, second + 1, direction),
                                  book(second + 1, strike_price=strike, strike_source=source),
                                  now=second, wall_time=EPOCH + second))
    return engine, events


class FloorBandTests(unittest.TestCase):
    def test_asymmetric_percentage_limits_for_both_trade_directions(self):
        for side in ("up", "down"):
            for mid, allowed in ((99800, True), (99900, True), (100050, True),
                                 (99799.9, False), (100050.1, False)):
                with self.subTest(side=side, mid=mid):
                    engine, events = entry(mid, side=side, max_above_floor_percent=.05,
                                           max_below_floor_percent=.2)
                    self.assertEqual(bool(events), allowed)
                    self.assertEqual(engine.position is not None, allowed)

    def test_point_one_means_one_tenth_percent_not_ten_percent(self):
        self.assertTrue(entry(100100)[1])
        self.assertFalse(entry(100100.1)[1])
        self.assertFalse(entry(101000)[1])

    def test_zero_side_limit_blocks_only_that_side_of_the_floor(self):
        self.assertTrue(entry(100000, max_above_floor_percent=0)[1])
        self.assertTrue(entry(99950, max_above_floor_percent=0)[1])
        self.assertFalse(entry(100000.01, max_above_floor_percent=0)[1])

    def test_missing_or_unverified_floor_blocks_even_without_trend_filter(self):
        for strike, source in ((None, None), (0, "kalshi.floor_strike"),
                               (float("nan"), "kalshi.floor_strike"), (100000, "guessed")):
            with self.subTest(strike=strike, source=source):
                engine, events = entry(100000, strike=strike, source=source)
                self.assertEqual(events, [])
                self.assertIn("verified", engine.snapshot()["message"])

    def test_disabled_filter_preserves_prior_behavior_without_reference(self):
        engine = PaperEngine(PaperConfig(require_toward_strike=False))
        events = []
        for second in range(21):
            events.extend(engine.step(feeds(65000 + second * 4, second + 1), book(second + 1),
                                      now=second, wall_time=EPOCH + second))
        self.assertEqual(len(events), 1)

    def test_band_does_not_block_a_held_positions_profit_exit(self):
        engine, _ = entry(100000)
        events = engine.step(feeds(110000, 22), book(22, yes=[[.95, 20]], no=[[.04, 20]],
                             strike_price=100000, strike_source="kalshi.floor_strike"),
                             now=21, wall_time=EPOCH + 21)
        self.assertEqual(events[0]["reason"], "hard_95c")

    def test_previous_24_field_journal_preserves_account_position_and_history(self):
        engine, _ = entry(100000)
        state = engine.dump_state()
        for rules in (state["config"], state["position"]["rules"], state["events"][0]["rules"]):
            for key in FLOOR_BAND_FIELDS:
                rules.pop(key)
            self.assertEqual(len(rules), 24)
        original = copy.deepcopy(state)
        restored = PaperEngine(state=state)
        self.assertFalse(restored.config.floor_distance_enabled)
        self.assertEqual(restored.cash, original["cash"])
        self.assertEqual(restored.events[0], original["events"][0])
        self.assertEqual(restored.position, original["position"])
        invalid = restored.dump_state()
        invalid["config"].pop("max_above_floor_percent")
        with self.assertRaises(ValueError):
            PaperEngine(state=invalid)

    def test_percentages_are_finite_nonnegative_and_bounded(self):
        for key in ("max_above_floor_percent", "max_below_floor_percent"):
            for value in (-.001, 100.001, float("nan"), float("inf"), True, "0.1"):
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    PaperConfig(**{key: value})


class FloorBandApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_saved_percentages_survive_restart_without_changing_cash_or_history(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "paper.sqlite3"

            async def start():
                app = create_app(Settings(), run_feeds=False, paper_path=path)
                client = await self.enterAsyncContext(TestClient(TestServer(app)))
                async with asyncio.timeout(5):
                    while not app[STORE].paper_control.available:
                        await asyncio.sleep(.01)
                return client, app

            client, app = await start()
            response = await client.get("/api/paper/settings")
            before = await response.json()
            response = await client.put("/api/paper/settings", json={
                "settings": {"floor_distance_enabled": True, "max_above_floor_percent": .25,
                             "max_below_floor_percent": .075}, "revision": before["revision"]})
            self.assertEqual(response.status, 200)
            saved = await response.json()
            cash = app[STORE].paper["cash"]
            response = await client.put("/api/paper/settings", json={
                "settings": {"max_above_floor_percent": -1}, "revision": saved["revision"]})
            self.assertEqual(response.status, 400)
            await client.close()
            reopened, restored = await start()
            response = await reopened.get("/api/paper/settings")
            self.assertEqual((await response.json())["settings"], saved["settings"])
            self.assertEqual(restored[STORE].paper["cash"], cash)
            self.assertEqual(restored[STORE].paper["events"], [])
            await reopened.close()

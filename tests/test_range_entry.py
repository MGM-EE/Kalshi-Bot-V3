"""Optional entry weights use causal range context; exits keep their own rules."""
import asyncio
import copy
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from aiohttp.test_utils import TestClient, TestServer

from feedboard.entry_filters import EntryQuality, entry_requirements
from feedboard.paper import PaperConfig, PaperEngine, RANGE_FIELDS
from feedboard.paper_runtime import PaperJournal
from feedboard.paper_conditions import _entry_conditions
from feedboard.strategy_settings import apply_public_settings, public_settings
from feedboard.web import STORE, create_app
from tests.paper_fixtures import baseline_config
from tests.test_paper import EPOCH, book, feeds, warm
from tests.test_entry_filters import tick


def context(**changes):
    return {"ready": True, "lower": 64950., "upper": 65450., "position": .1,
            "favored": "up", "regime": "range", **changes}


def signal(direction=1, five=.65, twenty=.9):
    return {"midpoint": 65000., "five": direction * five, "twenty": direction * twenty,
            "floor5": .75, "floor20": 1.}


def attempt(enabled, observed, *, direction=1, five=.65, twenty=.9):
    engine = PaperEngine(baseline_config(require_toward_strike=False, range_bias_enabled=enabled,
                                       flow_imbalance=.25))
    observed_signal = signal(direction, five, twenty)
    with patch.object(engine._range_context, "view", return_value=observed), \
            patch.object(engine, "_observe", return_value=observed_signal):
        events = engine.step(feeds(65000, 1, direction), book(), now=0, wall_time=EPOCH + 360)
        # An immediate entry switches the public panel to exit checks. Inspect
        # the entry explanation directly without changing the account.
        indicators = {r["key"]: r for r in _entry_conditions(engine, engine._condition_context)["items"]}
    return engine, events, indicators, observed_signal


class RangeEntryTests(unittest.TestCase):
    def test_observation_default_does_not_change_entry_thresholds(self):
        self.assertFalse(PaperConfig().range_bias_enabled)
        engine, events, indicators, _ = attempt(False, context())
        self.assertEqual(events, [])
        self.assertEqual(indicators["range_bias"]["state"], "disabled")
        self.assertEqual(engine._condition_context["entry_requirements"]["up"]["range_factor"], 1)

    def test_trusted_lower_and_upper_boundaries_favor_opposite_sides(self):
        for direction, observed in ((1, context()), (-1, context(position=.9, favored="down"))):
            with self.subTest(direction=direction):
                engine, events, indicators, original = attempt(True, observed, direction=direction)
                side = "up" if direction == 1 else "down"
                self.assertEqual(events[0]["side"], side)
                requirement = engine._condition_context["entry_requirements"][side]
                self.assertAlmostEqual(requirement["momentum5_bps"], .6)
                self.assertAlmostEqual(requirement["flow_imbalance"], .2)
                self.assertEqual(indicators["momentum"]["state"], "met")
                self.assertIn("0.80×", indicators["momentum"]["detail"])
                self.assertEqual(original, signal(direction))
                self.assertEqual(engine._condition_context["signal"], original)

    def test_opposing_and_middle_range_require_stronger_momentum(self):
        for observed in (context(position=.9, favored="down"), context(position=.5, favored=None)):
            _, base, _, _ = attempt(False, observed, five=.8, twenty=1.05)
            engine, biased, indicators, _ = attempt(True, observed, five=.8, twenty=1.05)
            self.assertEqual(len(base), 1)
            self.assertEqual(biased, [])
            self.assertNotEqual(indicators["momentum"]["state"], "met")
            self.assertGreater(engine._condition_context["entry_requirements"]["up"]["range_factor"], 1)

    def test_stale_warmup_and_breakout_context_are_neutral(self):
        samples = [context(ready=False), context(ready=False, regime="unavailable")]
        samples.extend(context(regime=regime) for regime in
                       ("outside_range", "pending_breakout_up", "pending_breakout_down", "breakout_up", "breakout_down"))
        for observed in samples:
            with self.subTest(observed=observed):
                engine, events, _, _ = attempt(True, observed)
                self.assertEqual(events, [])
                self.assertEqual(engine._condition_context["entry_requirements"]["up"]["range_factor"], 1)
                self.assertEqual(len(attempt(True, observed, five=2, twenty=3)[1]), 1)

    def test_factor_changes_restart_confirmation_without_changing_signal(self):
        engine = PaperEngine(PaperConfig(choppiness_enabled=False, range_bias_enabled=True))
        observed = context()
        with patch.object(engine._range_context, "view", return_value=observed):
            self.assertEqual(tick(engine, 0), [])
            observed.update(position=.5, favored=None)
            self.assertEqual(tick(engine, 1), [])
            self.assertEqual(engine._entry_quality.since, 1)
            self.assertEqual(tick(engine, 2), [])
            self.assertEqual(len(tick(engine, 3)), 1)

    def test_range_bias_does_not_rewrite_position_exits_or_dynamic_targets(self):
        engines = [warm(PaperEngine(baseline_config(require_toward_strike=False, range_bias_enabled=value)))[0]
                   for value in (False, True)]
        emitted = []
        for engine in engines:
            original = copy.deepcopy(engine.position)
            with patch.object(engine._range_context, "view", return_value=context(position=.9, favored="down")):
                events = engine.step(feeds(65084, 22), book(22, yes=[[.95, 20]], no=[[.04, 20]]),
                                     now=21, wall_time=EPOCH + 21)
            self.assertEqual(events[0]["reason"], "hard_95c")
            self.assertIsNone(engine._condition_context["entry_requirements"])
            self.assertEqual(engine.events[0]["rules"]["stop_loss"], original["rules"]["stop_loss"])
            emitted.append(events)
        self.assertEqual(emitted[0], emitted[1])
        self.assertEqual(engines[0].cash, engines[1].cash)

    def test_strength_is_bounded_and_flow_requirement_cannot_exceed_one(self):
        for strength in (-.001, .251, True, None, float("nan")):
            with self.subTest(strength=strength), self.assertRaises(ValueError):
                PaperConfig(range_bias_strength=strength)
        for value in (0, 1, "true", None):
            with self.assertRaises(ValueError):
                PaperConfig(range_bias_enabled=value)
        config = PaperConfig(range_bias_enabled=True, flow_imbalance=.95)
        threshold = entry_requirements(config, signal(), "down", context())
        self.assertEqual(threshold["flow_imbalance"], 1)
        self.assertEqual(apply_public_settings(config, {"range_bias_percent": 25}).range_bias_strength, .25)

    def test_old_34_field_account_and_position_migrate_with_bias_off(self):
        engine, _ = warm()
        state = engine.dump_state()
        for rules in (state["config"], state["position"]["rules"], state["events"][0]["rules"]):
            for name in RANGE_FIELDS:
                rules.pop(name)
        original = copy.deepcopy(state)
        restored = PaperEngine(state=state)
        self.assertFalse(restored.config.range_bias_enabled)
        self.assertEqual(restored.cash, original["cash"])
        self.assertEqual(restored.position, original["position"])
        self.assertEqual(list(restored.events), original["events"])
        state["config"]["range_bias_enabled"] = False
        with self.assertRaises(ValueError):
            PaperEngine(state=state)

    def test_signed_trend_diagnostic_distinguishes_opposing_straight_paths(self):
        for direction in (-1, 1):
            quality = EntryQuality()
            for second in range(61):
                quality.observe(100 + direction * second, second, second, EPOCH + second)
            self.assertEqual(quality.trend()["efficiency"], 1)
            self.assertEqual(quality.trend()["signed_efficiency"], direction)
            self.assertAlmostEqual(quality.trend()["net_change_bps"], direction * 6000)
        quality = EntryQuality()
        for second in range(61):
            quality.observe(1e-300 if second == 0 else 1e308, second, second, EPOCH + second)
        self.assertIsNone(quality.trend()["net_change_bps"])


class RangeSettingsApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_optional_weights_save_and_survive_restart_without_orders(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "paper.sqlite3"

            async def start():
                app = create_app(run_feeds=False, paper_path=path)
                client = await self.enterAsyncContext(TestClient(TestServer(app)))
                async with asyncio.timeout(5):
                    while not app[STORE].paper_control.available:
                        await asyncio.sleep(.01)
                return client

            client = await start()
            initial = await (await client.get("/api/paper/settings")).json()
            self.assertFalse(initial["settings"]["range_bias_enabled"])
            response = await client.put("/api/paper/settings", json={
                "revision": initial["revision"], "settings": {"range_bias_enabled": True, "range_bias_percent": 15}})
            self.assertEqual(response.status, 200)
            saved = await response.json()
            before = PaperJournal(path).load()
            response = await client.put("/api/paper/settings", json={
                "revision": saved["revision"], "settings": {"range_bias_percent": 26}})
            self.assertEqual(response.status, 400)
            self.assertEqual(PaperJournal(path).load(), before)
            await client.close()
            reopened = await start()
            current = await (await reopened.get("/api/paper/settings")).json()
            self.assertEqual(current["settings"], saved["settings"])
            self.assertEqual(PaperJournal(path).load()[0]["cash"], 1000)
            self.assertEqual(PaperJournal(path).recent_events(), [])
            await reopened.close()

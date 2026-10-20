"""Causal entry gates, live settings, and old-account compatibility."""
import asyncio
import copy
import json
import tempfile
import unittest
from dataclasses import replace
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch

from aiohttp.test_utils import TestClient, TestServer

from feedboard.entry_filters import EntryQuality
from feedboard.paper import ENTRY_FILTER_FIELDS, RANGE_FIELDS, PaperConfig, PaperEngine
from feedboard.paper_runtime import PaperJournal
from feedboard.strategy_settings import apply_public_settings, public_settings
from feedboard.web import STORE, create_app
from tests.test_paper import EPOCH, book, feeds, iso, warm

SIGNAL = {"midpoint": 65000., "five": 2., "twenty": 3., "floor5": .75, "floor20": 1.}


def tick(engine, now, *, wall=None, quote=None, depth=None):
    data = quote or feeds(65000, now + 1)
    current = depth or book(int(now * 10) + 1, strike_price=66000, strike_source="kalshi.floor_strike")
    with patch.object(engine, "_observe", return_value=SIGNAL):
        return engine.step(data, current, now=now, wall_time=EPOCH + 360 + now if wall is None else wall)


def confirming(**values):
    return PaperEngine(PaperConfig(choppiness_enabled=False, **values))


def indicators(engine):
    return {r["key"]: r for r in engine.snapshot()["conditions"]["items"]}


class WindowTests(unittest.TestCase):
    def test_start_is_inclusive_end_exclusive_and_90_second_floor_remains(self):
        for second, allowed in ((299.999, False), (300, True), (779.999, True), (780, False), (810, False)):
            with self.subTest(second=second):
                engine = confirming(confirmation_enabled=False)
                self.assertEqual(bool(tick(engine, 0, wall=EPOCH + second)), allowed)
                if not allowed:
                    self.assertEqual(indicators(engine)["market"]["state"], "blocked")
        for second, allowed in ((20, True), (810, True), (810.001, False)):
            engine = confirming(entry_window_enabled=False, confirmation_enabled=False)
            self.assertEqual(bool(tick(engine, 0, wall=EPOCH + second)), allowed)

    def test_fractional_custom_window_and_closed_market(self):
        config = PaperConfig(entry_start_minute=6.5, entry_end_minute=10,
                             confirmation_enabled=False, choppiness_enabled=False)
        for second, allowed in ((389.999, False), (390, True), (599.999, True), (600, False)):
            self.assertEqual(bool(tick(PaperEngine(config), 0, wall=EPOCH + second)), allowed)
        engine = PaperEngine(config)
        self.assertEqual(tick(engine, 0, wall=EPOCH + 400, depth=book(market_status="closed")), [])


class ConfirmationTests(unittest.TestCase):
    def test_requires_two_seconds_and_new_quote_and_book_observations(self):
        engine = confirming()
        self.assertEqual(tick(engine, 0), [])
        self.assertEqual(indicators(engine)["confirmation"]["state"], "building")
        self.assertEqual(tick(engine, 1), [])
        self.assertEqual(len(tick(engine, 2)), 1)
        self.assertEqual(engine.position["side"], "up")
        self.assertEqual(engine._condition_context["entry_quality"]["confirmation_elapsed_seconds"], 2)
        for stale in ("quote", "book"):
            with self.subTest(stale=stale):
                engine = confirming()
                for second in (0, 1, 2):
                    data = feeds(65000, 1 if stale == "quote" else second + 1)
                    depth = book(1 if stale == "book" else second + 1,
                                 strike_price=66000, strike_source="kalshi.floor_strike")
                    self.assertEqual(tick(engine, second, quote=data, depth=depth), [])
                self.assertEqual(len(tick(engine, 2.1)), 1)

    def test_any_failed_gate_restarts_confirmation(self):
        for failure in ("kraken", "kalshi", "book", "flow", "depth", "spread", "strike", "price"):
            with self.subTest(failure=failure):
                engine = confirming()
                self.assertEqual(tick(engine, 0), [])
                data = feeds(65000, 2)
                depth = book(2, strike_price=66000, strike_source="kalshi.floor_strike")
                if failure in ("kraken", "kalshi"):
                    data[failure]["age_ms"] = 3001
                elif failure == "book":
                    depth["age_ms"] = 3001
                elif failure == "flow":
                    data["kraken"]["data"]["flow"]["age_ms"] = 3001
                elif failure == "depth":
                    depth["no"] = [[.5, 1]]
                elif failure == "spread":
                    depth["yes"] = [[.40, 20]]
                elif failure == "strike":
                    depth["strike_source"] = "guessed"
                else:
                    depth.update(yes=[[.8, 20]], no=[[.19, 20]])
                self.assertEqual(tick(engine, 1, quote=data, depth=depth), [])
                self.assertIsNone(engine._entry_quality.key)
                self.assertEqual(tick(engine, 2), [])
                self.assertEqual(tick(engine, 3), [])
                self.assertEqual(len(tick(engine, 4)), 1)

    def test_market_floor_settings_and_direction_changes_restart_timer(self):
        for change in ("ticker", "floor", "settings"):
            with self.subTest(change=change):
                engine = confirming()
                tick(engine, 0)
                tick(engine, 1)
                if change == "settings":
                    engine.set_config(replace(engine.config, flow_imbalance=.25))
                values = {"ticker": "KXBTC15M-NEXT"} if change == "ticker" else {}
                values["strike_price"] = 66001 if change == "floor" else 66000
                for second in (2, 3):
                    self.assertEqual(tick(engine, second, depth=book(second + 1, strike_source="kalshi.floor_strike", **values)), [])
                self.assertEqual(len(tick(engine, 4, depth=book(5, strike_source="kalshi.floor_strike", **values))), 1)
        quality = EntryQuality()
        quality.last_step = 0
        self.assertFalse(quality.confirm(("ticker", "up"), 0, 1, 1, 2))
        self.assertFalse(quality.confirm(("ticker", "down"), 2, 2, 2, 2))
        self.assertEqual(quality.since, 2)

    def test_pause_and_wall_clock_jump_do_not_count_as_confirmation(self):
        engine = confirming()
        tick(engine, 0)
        tick(engine, .5)
        self.assertEqual(tick(engine, 2), [])
        self.assertEqual(engine._entry_quality.since, 2)
        self.assertEqual(tick(engine, 3), [])
        self.assertEqual(len(tick(engine, 4)), 1)
        engine = confirming()
        tick(engine, 0)
        self.assertEqual(tick(engine, 1, wall=EPOCH + 365), [])
        self.assertEqual(tick(engine, 2, wall=EPOCH + 366), [])
        self.assertEqual(len(tick(engine, 3, wall=EPOCH + 367)), 1)

    def test_pausing_entries_and_expiring_window_clear_pending_setup(self):
        engine = confirming()
        tick(engine, 0)
        engine.set_config(replace(engine.config, entries_enabled=False))
        tick(engine, 1)
        self.assertIsNone(engine._entry_quality.key)
        engine = confirming()
        tick(engine, 0, wall=EPOCH + 779)
        self.assertEqual(tick(engine, 1, wall=EPOCH + 780), [])
        self.assertIsNone(engine._entry_quality.key)

    def test_choppiness_failure_restarts_an_otherwise_valid_setup(self):
        engine = PaperEngine()
        quality = {"efficiency": 1., "coverage_seconds": 60, "points": 61}
        with patch.object(engine._entry_quality, "trend", return_value=quality):
            self.assertEqual(tick(engine, 0), [])
            quality["efficiency"] = .2
            self.assertEqual(tick(engine, 1), [])
            self.assertIsNone(engine._entry_quality.key)
            quality["efficiency"] = 1.
            self.assertEqual(tick(engine, 2), [])
            self.assertEqual(tick(engine, 3), [])
            self.assertEqual(len(tick(engine, 4)), 1)


class TrendTests(unittest.TestCase):
    def test_complete_fresh_minute_plus_confirmation_is_required_for_both_sides(self):
        for direction in (1, -1):
            with self.subTest(direction=direction):
                engine = PaperEngine()
                events = []
                for second in range(63):
                    emitted = engine.step(feeds(65000 + direction * second * 4, second + 1, direction),
                                          book(second + 1, strike_price=66000 if direction==1 else 64000,
                                               strike_source="kalshi.floor_strike"),
                                          now=second, wall_time=EPOCH + 300 + second)
                    if second < 62:
                        self.assertEqual(emitted, [])
                    events.extend(emitted)
                self.assertEqual(len(events), 1)
                self.assertEqual(events[0]["side"], "up" if direction==1 else "down")
                self.assertEqual(engine._entry_quality.trend()["efficiency"], 1)

    def test_choppy_and_flat_paths_wait_even_with_other_signals_met(self):
        for path in (lambda n:65000, lambda n:65000+(20 if n%2 else 0)):
            engine = PaperEngine()
            for second in range(65):
                self.assertEqual(tick(engine, second, quote=feeds(path(second),second+1)), [])
            self.assertEqual(indicators(engine)["choppiness"]["state"], "blocked")
            self.assertIsNone(engine.position)

    def test_exact_efficiency_threshold_and_bounded_history(self):
        q = EntryQuality()
        for second in range(61):
            price = 100 + (second if second <= 40 else 80-second)
            q.observe(price, second, second, EPOCH + second)
        self.assertEqual(q.trend()["efficiency"], 1/3)
        engine = PaperEngine(PaperConfig(confirmation_enabled=False, min_trend_efficiency=1/3))
        engine._entry_quality = q
        with patch.object(q, "observe"):
            self.assertEqual(len(tick(engine, 60)), 1)
        strict = PaperEngine(PaperConfig(confirmation_enabled=False, min_trend_efficiency=1/3 + .000001))
        strict._entry_quality = copy.deepcopy(q)
        with patch.object(strict._entry_quality, "observe"):
            self.assertEqual(tick(strict, 60), [])
            self.assertNotEqual(indicators(strict)["choppiness"]["state"], "met")
        for second in range(61, 5000):
            q.observe(100 + second, second, second, EPOCH + second)
        self.assertLessEqual(len(q.points), 66)

    def test_stale_data_rest_fallback_quote_gaps_and_restart_discard_history(self):
        for invalid in ("stale", "rest", "gap", "clock"):
            q = EntryQuality()
            for second in range(61):
                q.observe(100+second,second,second,EPOCH+second)
            if invalid in ("stale", "rest"):
                engine = PaperEngine()
                engine._entry_quality = q
                data = feeds(65000, 62)
                if invalid == "stale":data['kraken']['age_ms']=3001
                else:data['kraken']['transport']='rest'
                tick(engine,61,wall=EPOCH+61,quote=data)
            elif invalid == "gap":q.observe(162,62,65,EPOCH+65)
            else:q.observe(162,62,61,EPOCH+70)
            self.assertIsNone(q.trend()["efficiency"])
        engine = confirming()
        tick(engine,0)
        self.assertIsNotNone(engine._entry_quality.key)
        restored = PaperEngine(state=engine.dump_state())
        self.assertIsNone(restored._entry_quality.key)
        self.assertEqual(restored._entry_quality.trend()["points"],0)

    def test_read_only_views_do_not_build_history_or_confirmation(self):
        engine = confirming()
        tick(engine,0)
        before = copy.deepcopy(engine._entry_quality.__dict__)
        state = engine.dump_state()
        for _ in range(30):
            json.dumps(engine.snapshot(),allow_nan=False)
        self.assertEqual(engine._entry_quality.__dict__, before)
        self.assertEqual(engine.dump_state(),state)

    def test_new_entry_filters_do_not_block_hard_profit_or_risk_exits(self):
        engine, _ = warm()
        engine.set_config(PaperConfig())
        events = engine.step({},book(22,yes=[[.95,20]],no=[[.04,20]]),now=21,wall_time=EPOCH+21)
        self.assertEqual(events[0]['reason'],'hard_95c')
        engine, _ = warm()
        engine.set_config(PaperConfig())
        events = engine.step({},book(22,yes=[[.30,20]],no=[[.69,20]]),now=21,wall_time=EPOCH+21)
        self.assertEqual(events[0]['reason'],'stop_loss')


class EntrySettingsTests(unittest.TestCase):
    def test_defaults_units_validation_and_old_rules_migrate_without_rewriting_history(self):
        c=PaperConfig()
        self.assertTrue(c.entry_window_enabled and c.confirmation_enabled and c.choppiness_enabled)
        self.assertEqual((c.entry_start_minute,c.entry_end_minute,c.confirmation_seconds,c.min_trend_efficiency),(5,13,2,.35))
        changed=apply_public_settings(c,{'min_trend_efficiency_percent':42,'confirmation_seconds':3,
                                       'entry_start_minute':6,'entry_end_minute':12})
        self.assertEqual(changed.min_trend_efficiency,.42)
        for values in ({'entry_start_minute':13},{'entry_start_minute':7,'entry_end_minute':6}):
            with self.assertRaises(ValueError):apply_public_settings(c,values)
        for name in ENTRY_FILTER_FIELDS:
            for invalid in (None, 'yes', float('nan')):
                with self.subTest(name=name,invalid=invalid),self.assertRaises(ValueError):PaperConfig(**{name:invalid})
        engine,_=warm()
        state=engine.dump_state()
        for rules in (state['config'],state['position']['rules'],state['events'][0]['rules']):
            for name in (*ENTRY_FILTER_FIELDS, *RANGE_FIELDS):rules.pop(name)
        original=copy.deepcopy(state)
        restored=PaperEngine(state=state)
        self.assertTrue(restored.config.entry_window_enabled and restored.config.confirmation_enabled and restored.config.choppiness_enabled)
        self.assertEqual(restored.cash,original['cash'])
        self.assertEqual(restored.position,original['position'])
        self.assertEqual(list(restored.events),original['events'])
        self.assertEqual(restored.config.stop_loss,original['config']['stop_loss'])
        corrupt=copy.deepcopy(original)
        corrupt['config']['confirmation_enabled']=True
        with self.assertRaises(ValueError):PaperEngine(state=corrupt)

    def test_every_public_control_has_a_matching_unique_html_input(self):
        class Inputs(HTMLParser):
            def __init__(self):super().__init__();self.names=[]
            def handle_starttag(self,tag,attrs):
                if tag=='input':self.names.append(dict(attrs).get('name'))
        html=Inputs();html.feed(Path('static/index.html').read_text())
        for name in public_settings(PaperConfig()):self.assertEqual(html.names.count(name),1,name)


class EntrySettingsApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_controls_save_atomically_and_survive_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'paper.sqlite3'
            async def start():
                app=create_app(run_feeds=False,paper_path=path)
                client=await self.enterAsyncContext(TestClient(TestServer(app)))
                async with asyncio.timeout(5):
                    while not app[STORE].paper_control.available:await asyncio.sleep(.01)
                return client,app
            client,app=await start()
            before=await (await client.get('/api/paper/settings')).json()
            values={'entry_window_enabled':True,'entry_start_minute':6,'entry_end_minute':12,
                    'confirmation_enabled':True,'confirmation_seconds':3,
                    'choppiness_enabled':True,'min_trend_efficiency_percent':45}
            response=await client.put('/api/paper/settings',json={'settings':values,'revision':before['revision']})
            self.assertEqual(response.status,200)
            saved=await response.json()
            journal=PaperJournal(path).load()
            response=await client.put('/api/paper/settings',json={'settings':{'entry_end_minute':5},'revision':saved['revision']})
            self.assertEqual(response.status,400)
            self.assertEqual(PaperJournal(path).load(),journal)
            await client.close()
            reopened,_=await start()
            actual=await (await reopened.get('/api/paper/settings')).json()
            self.assertEqual(actual['settings'],saved['settings'])
            self.assertEqual(PaperJournal(path).load()[0]['cash'],1000)
            self.assertEqual(PaperJournal(path).recent_events(),[])
            await reopened.close()

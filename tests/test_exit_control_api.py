"""Exit-control HTTP integration with real, temporary paper journals only."""
import asyncio
import copy
import sqlite3
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from aiohttp.test_utils import TestClient, TestServer

from feedboard.paper import PaperConfig, PaperEngine
from feedboard.paper_runtime import PaperJournal
from feedboard.web import JOURNAL, STORE, create_app


PATH = "/api/paper/settings"
TICKER = "KXBTC15M-EXIT-CONTROLS-TEST"
DEFAULT_CONTROLS = {"hold_to_settlement": False, "exit_on_stop_loss": True,
                    "exit_on_reversal": True, "exit_on_max_hold": True,
                    "exit_before_expiry": True}


def iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def seeded_state():
    """A genuine synthetic entry, with the position aged enough for risk checks."""
    engine = PaperEngine()
    epoch = time.time() - 260
    for second in range(21):
        mid = 65000 + second * 4
        feeds = {
            "kalshi": {"status": "live", "age_ms": 0},
            "kraken": {"status": "live", "age_ms": 0, "transport": "websocket",
                       "updates": second + 1, "data": {
                           "symbol": "BTC/USD", "bid": mid - .5, "ask": mid + .5,
                           "flow": {"buy_usd": 9000, "sell_usd": 1000,
                                    "large_buy_usd": 2000, "large_sell_usd": 0,
                                    "count": 20, "age_ms": 0, "window_seconds": 20}}},
        }
        book = {"ticker": TICKER, "status": "live", "age_ms": 0,
                "version": second + 1, "yes": [[.48, 20]], "no": [[.50, 20]],
                "close_time": iso(epoch + 900), "market_status": "open",
                "strike_price": 66000, "strike_source": "kalshi.floor_strike"}
        engine.step(feeds, book, now=second, wall_time=epoch + second)
    if engine.position is None:
        raise AssertionError("Synthetic exit-control fixture did not enter")
    return engine.dump_state()


def identity(state):
    return {key: state["position"][key] for key in ("ticker", "opened_at")}


def controls(rules):
    return {key: rules[key] for key in DEFAULT_CONTROLS}


def observed_book(version=22, price=.95, quantity=5):
    return {"ticker": TICKER, "status": "live", "age_ms": 0, "version": version,
            "yes": [[price, quantity]], "no": [[round(1 - price - .01, 6), 5]],
            "market_status": "active", "settlement_result": None}


class ExitControlApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "paper.sqlite3"

    async def until(self, predicate):
        async with asyncio.timeout(5):
            while not predicate():
                await asyncio.sleep(.01)

    async def client_for(self, *, state=None, journal=None):
        if state is not None:
            PaperJournal(self.path).save(state, state["events"], 0)
        app = create_app(run_feeds=False, paper_path=self.path)
        if journal is not None:
            app[JOURNAL] = journal
        client = await self.enterAsyncContext(TestClient(TestServer(app)))
        await self.until(lambda: app[STORE].paper_control.available)
        return client, app

    async def document(self, client):
        response = await client.get(PATH)
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        return await response.json()

    async def save(self, client, settings, *, revision=0, target=None):
        payload = {"settings": settings, "revision": revision}
        if target is not None:
            payload["apply_exit_controls_to_position"] = target
        return await client.put(PATH, json=payload)

    async def test_complete_control_round_trip_is_durable_without_account_changes(self):
        client, _ = await self.client_for()
        document = await self.document(client)
        self.assertEqual(controls(document["settings"]), DEFAULT_CONTROLS)
        updated_controls = {key: not value for key, value in DEFAULT_CONTROLS.items()}
        full = {**document["settings"], **updated_controls}
        response = await self.save(client, full)
        self.assertEqual(response.status, 200)
        result = await response.json()
        self.assertFalse(result["applied_to_position"])
        self.assertEqual(result["settings"], full)
        saved, disk_revision = PaperJournal(self.path).load()
        self.assertEqual(controls(saved["config"]), updated_controls)
        self.assertEqual(saved["cash"], 1000)
        self.assertEqual(saved["event_seq"], 0)
        self.assertEqual(saved["events"], [])
        self.assertIsNone(saved["position"])
        self.assertEqual(disk_revision, 2)
        await client.close()
        reopened, _ = await self.client_for()
        self.assertEqual((await self.document(reopened))["settings"], full)
        self.assertEqual(PaperJournal(self.path).load(), (saved, disk_revision))

    async def test_each_exit_control_rejects_non_boolean_json_values(self):
        client, _ = await self.client_for()
        original = PaperJournal(self.path).load()
        for key in DEFAULT_CONTROLS:
            for value in (0, "true", None):
                with self.subTest(key=key, value=value):
                    response = await self.save(client, {key: value})
                    self.assertEqual(response.status, 400)
                    self.assertIn("true or false", (await response.json())["error"])
        self.assertEqual(PaperJournal(self.path).load(), original)

    async def test_future_only_save_keeps_open_rules_and_entry_history_frozen(self):
        original = seeded_state()
        client, app = await self.client_for(state=original)
        response = await self.save(client, {"hold_to_settlement": True,
                                             "exit_on_stop_loss": False,
                                             "stop_loss_cents": 4, "max_hold_seconds": 600})
        self.assertEqual(response.status, 200)
        self.assertFalse((await response.json())["applied_to_position"])
        saved, _ = PaperJournal(self.path).load()
        self.assertEqual(saved["position"], original["position"])
        self.assertEqual(saved["events"], original["events"])
        self.assertEqual(saved["cash"], original["cash"])
        self.assertTrue(saved["config"]["hold_to_settlement"])
        self.assertFalse(app[STORE].paper["position"]["exit_controls"]["hold_to_settlement"])
        self.assertEqual(PaperJournal(self.path).recent_events(), original["events"][::-1])

    async def test_legacy_nineteen_field_rules_can_apply_hold_and_restart_preserving_history(self):
        legacy = seeded_state()
        for rules in (legacy["config"], legacy["position"]["rules"], legacy["events"][0]["rules"]):
            for key in DEFAULT_CONTROLS:
                del rules[key]
            self.assertEqual(len(rules), 19)
        original = copy.deepcopy(legacy)
        client, app = await self.client_for(state=legacy)
        self.assertEqual(controls((await self.document(client))["settings"]), DEFAULT_CONTROLS)
        self.assertEqual(app[STORE].paper["position"]["exit_controls"], DEFAULT_CONTROLS)
        response = await self.save(client, {"hold_to_settlement": True}, target=identity(original))
        self.assertEqual(response.status, 200)
        self.assertTrue((await response.json())["applied_to_position"])
        committed, revision = PaperJournal(self.path).load()
        self.assertTrue(committed["position"]["rules"]["hold_to_settlement"])
        self.assertTrue(committed["config"]["hold_to_settlement"])
        for key, value in original["position"]["rules"].items():
            self.assertEqual(committed["position"]["rules"][key], value)
        self.assertEqual(committed["cash"], original["cash"])
        self.assertEqual(committed["event_seq"], original["event_seq"])
        self.assertEqual(committed["events"], original["events"])
        self.assertEqual(PaperJournal(self.path).recent_events(), original["events"][::-1])
        await client.close()
        reopened, reopened_app = await self.client_for()
        self.assertTrue((await self.document(reopened))["settings"]["hold_to_settlement"])
        self.assertTrue(reopened_app[STORE].paper["position"]["exit_controls"]["hold_to_settlement"])
        self.assertEqual(PaperJournal(self.path).load(), (committed, revision))

    async def test_unchanged_future_settings_still_commit_only_new_position_controls(self):
        original = seeded_state()
        original["config"]["hold_to_settlement"] = True
        original["config_revision"] = 7
        client, app = await self.client_for(state=original)
        response = await self.save(client, {"hold_to_settlement": True}, revision=7,
                                   target=identity(original))
        self.assertEqual(response.status, 200)
        result = await response.json()
        self.assertTrue(result["applied_to_position"])
        self.assertEqual(result["revision"], 7)
        committed, revision = PaperJournal(self.path).load()
        self.assertEqual(revision, 2)
        self.assertEqual({k: v for k, v in committed.items() if k != "position"},
                         {k: v for k, v in original.items() if k != "position"})
        preserved_position = {k: v for k, v in committed["position"].items()
                              if k not in {"rules", "exit_controls_revision", "exit_controls_updated_at",
                                           "exit_controls_config_revision"}}
        self.assertEqual(preserved_position, {k: v for k, v in original["position"].items() if k != "rules"})
        expected_rules = {**original["position"]["rules"], "hold_to_settlement": True}
        self.assertEqual(committed["position"]["rules"], expected_rules)
        self.assertEqual(committed["position"]["exit_controls_revision"], 1)
        self.assertEqual(committed["position"]["exit_controls_config_revision"], 7)
        self.assertTrue(app[STORE].paper["position"]["exit_controls"]["hold_to_settlement"])

    async def test_mismatched_trade_identity_conflicts_without_changing_any_settings(self):
        original = seeded_state()
        client, _ = await self.client_for(state=original)
        before = PaperJournal(self.path).load()
        for target in ({**identity(original), "ticker": "KXBTC15M-ANOTHER"},
                       {**identity(original), "opened_at": original["position"]["opened_at"] + .001}):
            with self.subTest(target=target):
                response = await self.save(client, {"hold_to_settlement": True}, target=target)
                self.assertEqual(response.status, 409)
                self.assertEqual(PaperJournal(self.path).load(), before)
                self.assertFalse((await self.document(client))["settings"]["hold_to_settlement"])

    async def test_closed_trade_identity_conflicts_instead_of_setting_future_defaults(self):
        closed = PaperEngine().dump_state()
        client, _ = await self.client_for(state=closed)
        before = PaperJournal(self.path).load()
        response = await self.save(client, {"hold_to_settlement": True},
                                   target={"ticker": TICKER, "opened_at": time.time()})
        self.assertEqual(response.status, 409)
        self.assertEqual(PaperJournal(self.path).load(), before)

    async def test_malformed_explicit_targets_return_bad_request(self):
        original = seeded_state()
        client, _ = await self.client_for(state=original)
        before = PaperJournal(self.path).load()
        valid = identity(original)
        invalid = (None, True, [], {}, {"ticker": TICKER}, {**valid, "extra": 1},
                   {**valid, "ticker": ""}, {**valid, "ticker": 1},
                   {**valid, "opened_at": "123"}, {**valid, "opened_at": True},
                   {**valid, "opened_at": -1}, {**valid, "opened_at": float("nan")},
                   {**valid, "opened_at": float("inf")}, {**valid, "opened_at": 10**1000})
        for target in invalid:
            with self.subTest(target=str(target)[:100]):
                response = await client.put(PATH, json={"settings": {"hold_to_settlement": True},
                                                       "revision": 0,
                                                       "apply_exit_controls_to_position": target})
                self.assertEqual(response.status, 400)
        self.assertEqual(PaperJournal(self.path).load(), before)

    async def test_explicitly_applied_hold_suppresses_all_risk_triggers_until_settlement(self):
        original = seeded_state()
        original["position"]["close_time"] = iso(time.time() + 20)
        client, _ = await self.client_for(state=original)
        response = await self.save(client, {"hold_to_settlement": True}, target=identity(original))
        self.assertEqual(response.status, 200)
        committed, _ = PaperJournal(self.path).load()
        await client.close()
        engine = PaperEngine(state=committed)
        signal = {"five": -10, "twenty": -20, "floor5": .75, "floor20": 1, "midpoint": 64000}
        book = observed_book(price=.30)
        with patch.object(engine, "_observe", return_value=signal), \
                patch.object(engine, "_flow", return_value={"signed": -1, "large": -1}):
            self.assertEqual(engine.step({}, book, now=0, wall_time=time.time()), [])
            self.assertIsNotNone(engine.position)
            self.assertEqual(engine.cash, original["cash"])
            self.assertEqual(engine.event_seq, 1)
            closed_at = datetime.fromisoformat(original["position"]["close_time"]).timestamp() + 1
            self.assertEqual(engine.step({}, book, now=1, wall_time=closed_at), [])
            self.assertEqual(engine.snapshot()["status"], "awaiting_settlement")
            event = engine.step({}, {**book, "market_status": "finalized", "settlement_result": "no"},
                                now=2, wall_time=closed_at + 1)[0]
        self.assertEqual(event["reason"], "verified_settlement")
        self.assertEqual(event["price"], 0)
        self.assertTrue(event["exit_controls"]["hold_to_settlement"])
        self.assertLess(event["pnl"], 0)
        self.assertIsNone(engine.position)
        self.assertEqual(engine.trade_count, 1)

    async def test_applied_hold_preserves_profit_and_hard_95_cent_exits(self):
        original = seeded_state()
        client, _ = await self.client_for(state=original)
        response = await self.save(client, {"hold_to_settlement": True}, target=identity(original))
        self.assertEqual(response.status, 200)
        committed, _ = PaperJournal(self.path).load()
        await client.close()
        for price, reason in ((.94, "dynamic_target"), (.95, "hard_95c")):
            with self.subTest(price=price):
                engine = PaperEngine(state=committed)
                event = engine.step({}, observed_book(price=price), now=0, wall_time=time.time())[0]
                self.assertEqual(event["reason"], reason)
                self.assertTrue(event["exit_controls"]["hold_to_settlement"])
                self.assertIsNone(engine.position)

    async def test_partial_exit_audit_uses_effective_position_policy_and_keeps_entry_truth(self):
        original = seeded_state()
        client, app = await self.client_for(state=original)
        response = await self.save(client, {"hold_to_settlement": True}, target=identity(original))
        self.assertEqual(response.status, 200)
        store = app[STORE]
        store.book, store.book_mono = observed_book(quantity=2), time.monotonic()
        await self.until(lambda: store.paper.get("last_event", {}).get("id") == 2)
        first_exit = PaperJournal(self.path).recent_events()[0]
        self.assertEqual(first_exit["contracts"], 2)
        self.assertEqual(store.paper["position"]["contracts"], 3)
        self.assertTrue(first_exit["exit_controls"]["hold_to_settlement"])
        self.assertEqual(first_exit["exit_controls_revision"], 1)
        response = await self.save(client, {"hold_to_settlement": False}, revision=1)
        self.assertEqual(response.status, 200)
        self.assertFalse((await response.json())["applied_to_position"])
        store.book, store.book_mono = observed_book(version=23, quantity=3), time.monotonic()
        await self.until(lambda: store.paper.get("last_event", {}).get("id") == 3)
        history_response = await client.get("/api/paper/trades")
        history = (await history_response.json())["events"]
        self.assertEqual([e["id"] for e in history], [3, 2, 1])
        self.assertTrue(history[0]["exit_controls"]["hold_to_settlement"])
        self.assertFalse(history[-1]["rules"]["hold_to_settlement"])
        self.assertEqual(history[-1], original["events"][0])
        self.assertIsNone(store.paper["position"])

    async def test_position_only_apply_waits_for_commit_before_publishing(self):
        original = seeded_state()
        original["config"]["hold_to_settlement"] = True
        original["config_revision"] = 1
        loop = asyncio.get_running_loop()
        saving, release = asyncio.Event(), threading.Event()

        class BlockingJournal(PaperJournal):
            def save(self, state, events, revision):
                if state["position"]["rules"]["hold_to_settlement"]:
                    loop.call_soon_threadsafe(saving.set)
                    if not release.wait(5):
                        raise TimeoutError("Test did not release position settings commit")
                return super().save(state, events, revision)

        client, app = await self.client_for(state=original, journal=BlockingJournal(self.path))
        before = PaperJournal(self.path).load()
        request = asyncio.create_task(self.save(client, {"hold_to_settlement": True}, revision=1,
                                                target=identity(original)))
        try:
            await asyncio.wait_for(saving.wait(), 5)
            self.assertFalse(request.done())
            self.assertEqual(PaperJournal(self.path).load(), before)
            snapshot = await (await client.get("/api/snapshot")).json()
            self.assertFalse(snapshot["paper"]["position"]["exit_controls"]["hold_to_settlement"])
            self.assertFalse(app[STORE].paper_engine.position["rules"]["hold_to_settlement"])
            release.set()
            response = await asyncio.wait_for(request, 5)
            self.assertEqual(response.status, 200)
            self.assertTrue((await response.json())["applied_to_position"])
            self.assertTrue(app[STORE].paper["position"]["exit_controls"]["hold_to_settlement"])
            self.assertEqual(PaperJournal(self.path).load()[0]["events"], original["events"])
        finally:
            release.set()
            await asyncio.gather(request, return_exceptions=True)

    async def test_failed_position_apply_preserves_previous_state_and_pauses(self):
        original = seeded_state()
        original["config"]["hold_to_settlement"] = True
        original["config_revision"] = 1

        class FailingJournal(PaperJournal):
            def save(self, state, events, revision):
                if state["position"]["rules"]["hold_to_settlement"]:
                    raise sqlite3.OperationalError("Synthetic position-only save failure")
                return super().save(state, events, revision)

        client, app = await self.client_for(state=original, journal=FailingJournal(self.path))
        before = PaperJournal(self.path).load()
        with self.assertLogs("feedboard.paper_runtime", level="ERROR"):
            response = await self.save(client, {"hold_to_settlement": True}, revision=1,
                                       target=identity(original))
        self.assertEqual(response.status, 503)
        self.assertEqual(PaperJournal(self.path).load(), before)
        self.assertFalse((await self.document(client))["available"])
        self.assertEqual(app[STORE].paper["status"], "paused")
        self.assertFalse(app[STORE].paper["position"]["exit_controls"]["hold_to_settlement"])
        self.assertEqual(PaperJournal(self.path).recent_events(), original["events"][::-1])


if __name__ == "__main__":
    unittest.main()

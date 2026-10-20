"""Causal paper observations, delta recovery, bounded persistence and replay."""
import asyncio
import copy
import json
import sqlite3
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from feedboard.core import Settings, Store
from feedboard.decision_trace import DecisionDecoder, DecisionEncoder, decision_frame
from feedboard.paper import PaperConfig, PaperEngine
from feedboard.paper_runtime import PaperJournal, run_paper
from feedboard.research import ResearchRecorder
from tests.test_paper import EPOCH, book, feeds
from tests.test_paper_runtime import executable_exit, open_account


def drain(recorder):
    records = []
    while not recorder.queue.empty():
        records.append(recorder.queue.get_nowait())
    return records


def observe(engine, tick, *, exit_now=False):
    """Realistic 100ms decisions with distinct spot/book updates once a second."""
    second = tick // 10
    source = feeds(65000 + second * 4, second + 1)
    source["kraken"]["age_ms"] = tick % 10 * 100
    source["kraken"]["data"]["flow"]["age_ms"] = tick % 10 * 100
    depth = book(second + 1, strike_price=66000, strike_source="kalshi.floor_strike")
    if exit_now:
        depth.update(version=10000 + tick, yes=[[.95, 20]], no=[[.04, 20]])
    return engine.step(source, depth, now=tick / 10, wall_time=EPOCH + 300 + tick / 10)


class DecisionTraceTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.recorder = ResearchRecorder(Store(Settings()), Path(directory.name) / "research.sqlite3")

    def test_full_and_delta_reconstruct_exact_finite_numeric_inputs_without_snapshot(self):
        engine = PaperEngine()
        self.recorder.initialize_decisions(engine)
        expected = []
        with patch.object(engine, "snapshot", side_effect=AssertionError("No rendering in decision tracing")):
            for tick in range(31):
                events = observe(engine, tick)
                engine._condition_context["range_context"]["invalid_test_value"] = float("nan")
                self.recorder.record_decision(engine, events)
                frame = decision_frame(engine, events)
                frame["range_context"]["invalid_test_value"] = None
                expected.append(frame)
        records = drain(self.recorder)
        self.assertEqual(records[0]["kind"], "decision_bootstrap")
        traces = [row["payload"] for row in records if row["kind"] == "decision"]
        self.assertEqual(len(traces), 31)
        self.assertTrue(any(row["encoding"] == "delta" for row in traces))
        decoder = DecisionDecoder()
        anchors = []
        for wanted, payload in zip(expected, traces):
            restored = decoder.decode(payload)
            self.assertEqual(restored, wanted)
            self.assertIsInstance(restored["feeds"]["kraken"]["data"]["bid"], (float, int))
            self.assertIn("entry_requirements", restored)
            self.assertIn("decision_phase", restored)
            self.assertIsNone(restored["range_context"]["invalid_test_value"])
            json.dumps(payload, allow_nan=False)
            if payload["encoding"] == "full":
                anchors.append(restored["now"])
        self.assertLessEqual(max(b - a for a, b in zip(anchors, anchors[1:])), 1.)

    def test_missing_delta_is_detectable_and_later_full_anchor_resyncs_context(self):
        encoder, decoder = DecisionEncoder(), DecisionDecoder()
        encoder.begin()
        rows = [encoder.encode({"now": tick / 10, "nested": {"value": tick}}) for tick in range(11)]
        decoder.decode(rows[0])
        with self.assertRaisesRegex(ValueError, "Missing decision trace base"):
            decoder.decode(rows[2])
        self.assertTrue(decoder.gap)
        full = next(row for row in rows[3:] if row["encoding"] == "full")
        restored = decoder.decode(full)
        self.assertEqual(restored["nested"]["value"], full["trace_sequence"] - 1)
        self.assertTrue(decoder.gap)
        self.assertEqual(decoder.decode(rows[full["trace_sequence"]])["nested"]["value"], full["trace_sequence"])

    def test_frame_copy_failure_reserves_sequences_and_exposes_the_missing_step(self):
        engine = PaperEngine()
        self.recorder.initialize_decisions(engine)
        self.recorder.record_decision(engine, observe(engine, 0))
        with patch("feedboard.research.decision_frame", side_effect=ValueError("synthetic frame failure")), \
                self.assertLogs("feedboard.research", level="ERROR"):
            self.recorder.record_decision(engine, observe(engine, 1))
        self.recorder.record_decision(engine, observe(engine, 2))
        records = drain(self.recorder)
        self.assertEqual([row["sequence"] for row in records], [1, 2, 4])
        self.assertEqual([row["payload"]["trace_sequence"] for row in records[1:]], [1, 3])
        self.assertEqual(records[-1]["payload"]["encoding"], "full")
        decoder = DecisionDecoder()
        decoder.decode(records[1]["payload"])
        decoder.decode(records[2]["payload"])
        self.assertTrue(decoder.gap)
        self.assertEqual(self.recorder.dropped_by_kind["decision"], 1)

    def test_nested_removals_and_empty_dicts_reconstruct(self):
        encoder, decoder = DecisionEncoder(), DecisionDecoder()
        first = {"now": 0., "a": {"b": {"c": 3}, "d": 2}, "empty": None}
        second = {"now": .1, "a": {"b": {}}, "empty": {}}
        decoder.decode(encoder.encode(first))
        self.assertEqual(decoder.decode(encoder.encode(second)), second)

    def test_queue_reserves_fill_space_and_dropped_trace_forces_new_anchor(self):
        recorder = ResearchRecorder(Store(Settings()), self.recorder.database.path, queue_limit=8)
        engine = PaperEngine()
        recorder.initialize_decisions(engine)
        for tick in range(10):
            recorder.record_decision(engine, observe(engine, tick))
        self.assertEqual(recorder.queue.qsize(), recorder.trace_capacity)
        self.assertGreater(recorder.dropped_by_kind["decision"], 0)
        self.assertTrue(recorder.enqueue("snapshot", {"sample": 1}))
        self.assertTrue(recorder.enqueue("snapshot", {"sample": 2}))
        self.assertFalse(recorder.enqueue("snapshot", {"sample": 3}))
        # Lower-priority observations have not consumed the exact-fill reserve.
        recorder.record_fill(engine, [{"id": 1, "ticker": "KXBTC15M-TEST"},
                                      {"id": 2, "ticker": "KXBTC15M-TEST"}])
        self.assertEqual(recorder.queue.qsize(), 8)
        self.assertNotIn("paper_fill", recorder.dropped_by_kind)
        before = drain(recorder)
        recorder.record_decision(engine, observe(engine, 10))
        after = drain(recorder)[0]["payload"]
        self.assertEqual(after["encoding"], "full")
        accepted = [row["payload"] for row in before if row["kind"] == "decision"]
        self.assertGreater(after["trace_sequence"], accepted[-1]["trace_sequence"] + 1)
        self.assertEqual(recorder.status()["dropped_records_by_kind"]["snapshot"], 1)

    def test_bootstrap_is_pre_step_and_committed_settings_start_a_new_epoch(self):
        engine = PaperEngine()
        initial = engine.dump_state()
        self.recorder.initialize_decisions(engine)
        self.recorder.record_decision(engine, observe(engine, 0))
        engine.set_config(replace(engine.config, contracts=3))
        updated = engine.dump_state()
        self.recorder.initialize_decisions(engine, reason="settings")
        self.recorder.record_decision(engine, observe(engine, 1))
        records = drain(self.recorder)
        self.assertEqual([row["kind"] for row in records],
                         ["decision_bootstrap", "decision", "decision_bootstrap", "decision"])
        self.assertEqual(records[0]["payload"]["state"], initial)
        self.assertEqual(records[2]["payload"]["state"], updated)
        self.assertEqual(records[2]["payload"]["reason"], "settings")
        self.assertEqual(records[2]["payload"]["before_trace_sequence"], 2)
        self.assertEqual(records[3]["payload"]["bootstrap_id"], 2)
        self.assertEqual(records[3]["payload"]["encoding"], "full")
        self.assertEqual(records[3]["payload"]["frame"]["config_revision"], 1)
        self.assertIn("not feature history", records[2]["payload"]["replay_scope"])

    def test_delayed_bootstrap_is_written_before_any_accepted_decision(self):
        recorder = ResearchRecorder(Store(Settings()), self.recorder.database.path, queue_limit=4)
        for _ in range(4):
            recorder.enqueue("paper_fill", {})
        engine = PaperEngine()
        recorder.initialize_decisions(engine)
        recorder.record_decision(engine, observe(engine, 0))
        self.assertIsNotNone(recorder.pending_bootstrap)
        drain(recorder)
        recorder.record_decision(engine, observe(engine, 1))
        records = drain(recorder)
        self.assertEqual([row["kind"] for row in records], ["decision_bootstrap", "decision"])
        self.assertEqual(records[-1]["payload"]["encoding"], "full")
        self.assertEqual(records[-1]["payload"]["trace_sequence"], 2)
        self.assertEqual(records[0]["payload"]["before_trace_sequence"], 1)

    def test_disabling_traces_preserves_fill_collection(self):
        recorder = ResearchRecorder(Store(Settings()), self.recorder.database.path, decision_traces=False)
        engine = PaperEngine()
        recorder.initialize_decisions(engine)
        recorder.record_decision(engine, observe(engine, 0))
        recorder.record_fill(engine, [{"id": 1}])
        self.assertEqual([row["kind"] for row in drain(recorder)], ["paper_fill"])
        self.assertFalse(recorder.status()["decision_traces"])

    def test_app_can_disable_decisions_while_retaining_snapshots_and_fills(self):
        from feedboard.web import RESEARCH, create_app

        app = create_app(run_feeds=False, research_path=self.recorder.database.path,
                         research_decision_traces=False)
        recorder = app[RESEARCH]
        self.assertFalse(recorder.decision_traces)
        engine = PaperEngine()
        recorder.initialize_decisions(engine)
        recorder.record_decision(engine, observe(engine, 0))
        recorder.enqueue("snapshot", {"sample": 1})
        recorder.record_fill(engine, [{"id": 1}])
        self.assertEqual([row["kind"] for row in drain(recorder)], ["snapshot", "paper_fill"])

    def test_uninterrupted_cold_start_trace_replays_real_confirmation_entry_exit_and_account(self):
        original = PaperEngine()
        self.recorder.initialize_decisions(original)
        records = drain(self.recorder)
        emitted = []
        for tick in range(750):
            if tick == 320:
                original.set_config(replace(original.config, contracts=3))
                self.recorder.initialize_decisions(original, reason="settings")
                records.extend(drain(self.recorder))
            events = observe(original, tick, exit_now=original.position is not None)
            emitted.extend(events)
            self.recorder.record_decision(original, events)
            records.extend(drain(self.recorder))
            if emitted and emitted[-1]["type"] == "exit":
                break
        self.assertEqual([event["type"] for event in emitted], ["entry", "exit"])
        self.assertGreater(original.trade_count, 0)
        bootstrap = records[0]["payload"]
        replay = PaperEngine(state=bootstrap["state"])
        decoder, reproduced = DecisionDecoder(), []
        saw_confirmation = False
        for record in records[1:]:
            if record["kind"] == "decision_bootstrap":
                checkpoint = record["payload"]
                self.assertEqual(checkpoint["reason"], "settings")
                # A settings checkpoint continues prior feature history. It is
                # deliberately not treated as an independent cold restart.
                replay.set_config(PaperConfig(**checkpoint["state"]["config"]))
                self.assertEqual(replay.dump_state(), checkpoint["state"])
                continue
            frame = decoder.decode(record["payload"])
            self.assertFalse(decoder.gap)
            events = replay.step(frame["feeds"], frame["book"], now=frame["now"], wall_time=frame["wall_time"])
            reproduced.extend(events)
            self.assertEqual([event["id"] for event in events], frame["attempted_event_ids"])
            self.assertFalse(frame["attempted_events_are_committed"])
            self.assertEqual(replay._condition_context["signal"], frame["signal"])
            self.assertEqual(replay._condition_context["entry_quality"], frame["entry_quality"])
            self.assertEqual(replay._condition_context["range_context"], frame["range_context"])
            saw_confirmation |= frame["entry_quality"]["confirmation_elapsed_seconds"] > 0
        self.assertTrue(saw_confirmation)
        self.assertEqual(reproduced[0]["contracts"], 3)
        self.assertEqual(reproduced, emitted)
        self.assertEqual(replay.dump_state(), original.dump_state())


class DecisionPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    async def until(self, predicate):
        async with asyncio.timeout(5):
            while not predicate():
                await asyncio.sleep(.005)

    async def test_writer_coalesces_step_records_off_thread_and_flushes_shutdown(self):
        recorder = ResearchRecorder(Store(Settings()), self.root / "research.sqlite3")
        batches, thread_ids = [], []
        real_write = recorder.database.write

        def write(batch):
            batches.append([row["sequence"] for row in batch])
            thread_ids.append(threading.get_ident())
            return real_write(batch)

        with patch.object(recorder.database, "write", side_effect=write):
            writer = asyncio.create_task(recorder.writer())
            await recorder.ready.wait()
            for index in range(10):
                recorder.enqueue("decision", {"index": index})
                await asyncio.sleep(.01)
            recorder.stop_event.set()
            await writer
        self.assertEqual(recorder.written, 10)
        self.assertEqual(sum(map(len, batches)), 10)
        self.assertLess(len(batches), 5)
        self.assertLessEqual(max(map(len, batches)), 50)
        self.assertTrue(all(identity != threading.get_ident() for identity in thread_ids))

    async def test_recorder_failure_cannot_freeze_paper_and_attempts_precede_commit(self):
        state = open_account().dump_state()
        path = self.root / "paper.sqlite3"
        PaperJournal(path).save(state, state["events"], 0)
        store = Store(Settings())
        executable_exit(store)
        calls = []

        class BrokenResearch:
            def initialize_decisions(self, engine, **kwargs):
                calls.append(("bootstrap", kwargs["reason"], engine.event_seq))
                raise RuntimeError("synthetic optional diagnostic failure")

            def record_decision(self, engine, events):
                calls.append(("attempt", [event["id"] for event in events]))
                raise RuntimeError("synthetic optional diagnostic failure")

            def record_fill(self, engine, events):
                persisted, _ = PaperJournal(path).load()
                calls.append(("committed", persisted["event_seq"]))
                raise RuntimeError("synthetic optional diagnostic failure")

        store.research = BrokenResearch()
        # Store.snapshot asks research for status; its diagnostics remain simple.
        store.research.status = lambda: {}
        with self.assertLogs("feedboard.paper_runtime", level="ERROR"):
            task = asyncio.create_task(run_paper(store, PaperJournal(path), PaperConfig()))
            try:
                await self.until(lambda: (store.paper.get("last_event") or {}).get("id") == 2)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(calls[:3], [("bootstrap", "startup", 1), ("attempt", [2]), ("committed", 2)])
        self.assertEqual(store.paper["status"], "watching")
        self.assertIsNone(PaperJournal(path).load()[0]["position"])

    async def test_journal_retry_never_emits_another_decision_or_claims_an_uncommitted_fill(self):
        state = open_account().dump_state()
        path = self.root / "paper.sqlite3"
        PaperJournal(path).save(state, state["events"], 0)
        store = Store(Settings())
        executable_exit(store)
        recorder = ResearchRecorder(store, self.root / "research.sqlite3")
        store.research = recorder
        attempts = []
        real_sleep = asyncio.sleep

        class RetryJournal(PaperJournal):
            def save(self, document, events, revision):
                attempts.append(copy.deepcopy((document, events, revision)))
                if len(attempts) == 1:
                    # At failure, the only new evidence is explicitly attempted.
                    queued = list(recorder.queue._queue)
                    self_decisions = [row for row in queued if row["kind"] == "decision"]
                    if len(self_decisions) != 1 or any(row["kind"] == "paper_fill" for row in queued):
                        raise AssertionError("Research claimed a committed fill before the journal")
                    raise sqlite3.OperationalError("synthetic retry")
                return super().save(document, events, revision)

        async def quick_retry(seconds):
            await real_sleep(0 if seconds == 5 else seconds)

        with patch("feedboard.paper_runtime.asyncio.sleep", quick_retry), \
                self.assertLogs("feedboard.paper_runtime", level="ERROR"):
            task = asyncio.create_task(run_paper(store, RetryJournal(path), PaperConfig()))
            try:
                await self.until(lambda: (store.paper.get("last_event") or {}).get("id") == 2)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(attempts[0], attempts[1])
        rows = drain(recorder)
        self.assertEqual([row["kind"] for row in rows], ["decision_bootstrap", "decision", "paper_fill"])
        self.assertEqual(rows[1]["payload"]["frame"]["attempted_event_ids"], [2])
        self.assertFalse(rows[1]["payload"]["frame"]["attempted_events_are_committed"])
        self.assertTrue(rows[-1]["payload"]["recorded_after_paper_commit"])

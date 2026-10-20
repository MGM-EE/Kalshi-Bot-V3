"""Portable exports preserve research, account history, outcomes, and WAL data."""
import json
import secrets
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from feedboard.export_research import Parts, export
from feedboard.paper_runtime import PaperJournal
from feedboard.research import ResearchDB, iso
from tests.test_paper_runtime import open_account
from tests.test_research import TICKER, record


def read_parts(paths):
    records = []
    for path in paths:
        with zipfile.ZipFile(path) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            if manifest["format"] != "pulse-research-jsonl":
                raise AssertionError("Invalid export format")
            records.extend(json.loads(line) for line in archive.read("research.jsonl").splitlines())
    return records


class ResearchExportTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.research, self.paper = self.root / "research.sqlite3", self.root / "paper.sqlite3"
        self.db = ResearchDB(self.research)
        self.db.initialize("session", {"started_at": iso()})
        self.db.write([record(1), record(2)])
        self.state = open_account().dump_state()
        PaperJournal(self.paper).save(self.state, self.state["events"], 0)

    def test_export_contains_snapshots_provenance_outcomes_and_authoritative_paper_history(self):
        paths = export(self.research, self.paper, self.root / "exports")
        data = read_parts(paths)
        self.assertEqual([row["sequence"] for row in data if row["type"] == "snapshot"], [1, 2])
        session = next(row for row in data if row["type"] == "session")
        self.assertEqual(len(session["metadata"]["source_sha256"]), 64)
        self.assertEqual(next(row for row in data if row["type"] == "market")["ticker"], TICKER)
        self.assertEqual(next(row for row in data if row["type"] == "paper_event")["event"], self.state["events"][0])
        self.assertEqual(next(row for row in data if row["type"] == "paper_account")["state"], self.state)
        self.assertEqual(data[-1]["type"], "export_complete")
        self.assertEqual(PaperJournal(self.paper).load(), (self.state, 1))

    def test_date_filter_is_utc_and_missing_data_is_not_a_successful_export(self):
        other = record(3, captured_at="2026-10-20T00:00:00.000+00:00")
        self.db.write([other])
        paths = export(self.research, None, self.root / "filtered", day="2026-10-19")
        self.assertEqual([row["sequence"] for row in read_parts(paths) if row["type"] == "snapshot"], [1, 2])
        with self.assertRaises(ValueError):
            export(self.research, None, self.root / "empty", day="2026-10-21")
        self.assertEqual(list((self.root / "empty").glob("*.zip")), [])

    def test_bounded_parts_are_independent_zips_with_no_records_missing(self):
        records = [record(index) for index in range(3, 300)]
        for row in records:
            row["payload"]["incompressible_test_evidence"] = secrets.token_hex(1500)
        self.db.write(records)
        paths = export(self.research, None, self.root / "parts", part_bytes=65536)
        self.assertGreater(len(paths), 1)
        data = read_parts(paths)
        self.assertEqual(len([row for row in data if row["type"] == "snapshot"]), 299)
        for path in paths:
            self.assertLess(path.stat().st_size, 65536 + 65536)
        ids = set()
        for number, path in enumerate(paths, 1):
            with zipfile.ZipFile(path) as archive:
                manifest = json.loads(archive.read("manifest.json"))
                bundle = json.loads(archive.read("bundle.json"))
                ids.add(manifest["export_id"])
                self.assertEqual(bundle, {"export_id": manifest["export_id"], "part": number,
                                          "total_parts": len(paths), "complete": True})
                self.assertEqual(manifest["research_coverage"]["records"], 299)
        self.assertEqual(ids, {data[-1]["export_id"]})

    def test_manifest_and_footer_describe_exact_sequence_type_and_paper_coverage(self):
        self.db.write([record(4, kind="decision", captured_at="2026-10-19T12:00:00.100+00:00")])
        paths = export(self.research, self.paper, self.root / "coverage")
        with zipfile.ZipFile(paths[0]) as archive:
            manifest = json.loads(archive.read("manifest.json"))
        footer = read_parts(paths)[-1]
        self.assertEqual(manifest["export_id"], footer["export_id"])
        self.assertEqual(manifest["research_coverage"], footer["research_coverage"])
        coverage = manifest["research_coverage"]
        self.assertEqual(coverage["records"], 3)
        self.assertEqual(coverage["types"], {"snapshot": 2, "decision": 1})
        self.assertEqual(coverage["first_record_at"], "2026-10-19T12:00:00.000+00:00")
        self.assertEqual(coverage["last_record_at"], "2026-10-19T12:00:00.100+00:00")
        session = coverage["sessions"][0]
        self.assertEqual((session["session_id"], session["first_sequence"], session["last_sequence"]),
                         ("session", 1, 4))
        self.assertEqual(session["sequence_positions_without_exported_record"], 1)
        self.assertEqual(len(session["source_sha256"]), 64)
        paper = manifest["paper_coverage"]
        self.assertEqual(paper, footer["paper_coverage"])
        self.assertEqual(paper["event_count"], len(self.state["events"]))
        self.assertEqual(paper["state_event_seq"], self.state["event_seq"])
        self.assertEqual(paper["config_revision"], self.state["config_revision"])
        self.assertEqual(paper["journal_revision"], 1)
        self.assertEqual(paper["first_event_at"], self.state["events"][0]["at"])
        self.assertEqual(paper["last_event_id"], self.state["events"][-1]["id"])

    def test_manifests_whitelist_source_metadata_and_do_not_copy_arbitrary_session_fields(self):
        with self.db.connect() as connection:
            value = json.loads(connection.execute("SELECT metadata FROM sessions").fetchone()[0])
            value.update(api_token="do-not-print-secret", endpoint="https://user:password@example.invalid")
            connection.execute("UPDATE sessions SET metadata=?", (json.dumps(value),))
        paths = export(self.research, None, self.root / "safe-manifest")
        with zipfile.ZipFile(paths[0]) as archive:
            manifest = archive.read("manifest.json").decode()
        self.assertNotIn("do-not-print-secret", manifest)
        self.assertNotIn("password", manifest)

    def test_paper_snapshot_is_released_before_streaming_and_research_snapshot_stays_consistent(self):
        original_add, mutated = Parts.add, []

        def concurrent_update(parts, value):
            if not mutated:
                mutated.append(True)
                newer = {**self.state, "config_revision": 5}
                # This would fail with a locked DELETE-mode database if the
                # exporter held the paper snapshot during the ZIP stream.
                PaperJournal(self.paper).save(newer, [], 1)
                self.db.write([record(3)])
            original_add(parts, value)

        with patch.object(Parts, "add", concurrent_update):
            paths = export(self.research, self.paper, self.root / "snapshots")
        data = read_parts(paths)
        self.assertEqual([row["sequence"] for row in data if row["type"] == "snapshot"], [1, 2])
        account = next(row for row in data if row["type"] == "paper_account")
        self.assertEqual(account["state"], self.state)
        self.assertEqual(account["revision"], 1)
        self.assertEqual(data[-1]["paper_coverage"]["config_revision"], self.state["config_revision"])
        self.assertEqual(PaperJournal(self.paper).load()[1], 2)

    def test_reads_committed_wal_without_requiring_checkpoint_or_stopping_writer(self):
        connection = sqlite3.connect(self.research)
        self.addCleanup(connection.close)
        connection.execute("PRAGMA wal_autocheckpoint=0")
        connection.execute("UPDATE sessions SET summary=?", ('{"wal_evidence":true}',))
        connection.commit()
        self.assertTrue(Path(str(self.research) + "-wal").exists())
        data = read_parts(export(self.research, None, self.root / "wal"))
        session = next(row for row in data if row["type"] == "session")
        self.assertTrue(session["summary"]["wal_evidence"])

    def test_missing_paper_journal_fails_cleanly_and_research_only_is_explicit(self):
        with self.assertRaises(ValueError):
            export(self.research, self.root / "missing.sqlite3", self.root / "failed")
        self.assertEqual(list((self.root / "failed").glob("*.zip")), [])
        self.assertTrue(export(self.research, None, self.root / "research-only"))

    def test_export_never_overwrites_an_earlier_export(self):
        first = export(self.research, self.paper, self.root / "repeat")
        original = first[0].read_bytes()
        second = export(self.research, self.paper, self.root / "repeat")
        self.assertNotEqual(first, second)
        self.assertEqual(first[0].read_bytes(), original)
        with zipfile.ZipFile(first[0]) as older, zipfile.ZipFile(second[0]) as newer:
            self.assertNotEqual(json.loads(older.read("manifest.json"))["export_id"],
                                json.loads(newer.read("manifest.json"))["export_id"])

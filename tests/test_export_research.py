"""Portable exports preserve research, account history, outcomes, and WAL data."""
import json
import secrets
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path

from feedboard.export_research import export
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

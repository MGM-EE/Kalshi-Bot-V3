"""Export consistent read-only SQLite views into portable, bounded JSONL ZIPs.

Run after stopping the app: uv run python -m feedboard.export_research
Each part stands alone as a ZIP; send every part from an export together.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import tempfile
import uuid
import zipfile
import zlib
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .research import SCHEMA, clean, iso

PART_BYTES = 23 * 1024 * 1024
MAX_LINE_BYTES = 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 256 * 1024 * 1024
SESSION_PREVIEW_LIMIT = 100


def encode_line(value):
    line = (json.dumps(clean(value), allow_nan=False, separators=(",", ":")) + "\n").encode()
    if len(line) > MAX_LINE_BYTES:
        raise ValueError("A single research record exceeds the 1 MiB safety limit")
    return line


@contextmanager
def read_database(path):
    path = Path(path).resolve()
    if not path.is_file():
        raise ValueError(f"Database not found: {path}")
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2)
    try:
        # Include committed WAL transactions; copying just the .sqlite3 file
        # while running can miss them. Each database has its own read snapshot.
        connection.execute("BEGIN")
        yield connection
    finally:
        connection.rollback()
        connection.close()


class Parts:
    def __init__(self, directory, manifest, *, limit=PART_BYTES):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.manifest, self.limit = manifest, limit
        self.prefix = ("research-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-")
                       + manifest["export_id"][:6])
        self.paths = []
        self.archive = self.stream = None
        self.size = 0

    def add(self, value):
        line = encode_line(value)
        # Bound compressed upload size, rather than creating dozens of tiny ZIPs
        # from highly repetitive snapshots. Leave headroom for a final record,
        # the compressor's pending block, and ZIP directory overhead.
        if (self.archive is None or self.archive.fp.tell() >= self.limit
                or self.size + len(line) > MAX_UNCOMPRESSED_BYTES):
            self.close()
            path = self.directory / f"{self.prefix}-part{len(self.paths) + 1:03d}.zip"
            self.archive = zipfile.ZipFile(path, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=6)
            self.paths.append(path)
            self.archive.writestr("manifest.json", json.dumps({**self.manifest, "part": len(self.paths)}, indent=2))
            self.stream = self.archive.open("research.jsonl", "w")
            self.size = 0
        self.stream.write(line)
        self.size += len(line)

    def close(self):
        if self.stream is not None:
            self.stream.close()
            self.stream = None
        if self.archive is not None:
            self.archive.close()
            self.archive = None

    def complete(self):
        self.close()
        # Every part carries a small completion descriptor so a detached upload
        # can be matched to its bundle and missing parts are detectable.
        for number, path in enumerate(self.paths, 1):
            with zipfile.ZipFile(path, "a", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("bundle.json", json.dumps({"export_id": self.manifest["export_id"],
                                  "part": number, "total_parts": len(self.paths), "complete": True}))


def research_coverage(connection, where, parameters):
    count, first, last = connection.execute(
        "SELECT COUNT(*),MIN(captured_at),MAX(captured_at) FROM records" + where, parameters).fetchone()
    kinds = dict(connection.execute("SELECT kind,COUNT(*) FROM records" + where + " GROUP BY kind", parameters))
    preview, total = [], 0
    for session, rows, low, high, start, end in connection.execute(
            "SELECT session_id,COUNT(*),MIN(sequence),MAX(sequence),MIN(captured_at),MAX(captured_at)"
            " FROM records" + where + " GROUP BY session_id ORDER BY MIN(captured_at),session_id", parameters):
        total += 1
        if len(preview) < SESSION_PREVIEW_LIMIT:
            metadata = connection.execute("SELECT metadata FROM sessions WHERE session_id=?", (session,)).fetchone()
            fingerprint = json.loads(metadata[0]).get("source_sha256") if metadata else None
            # Manifests contain explicit provenance, never arbitrary session
            # metadata (which can include local paths or endpoint URLs).
            preview.append({"session_id": session, "records": rows, "first_sequence": low,
                            "last_sequence": high, "sequence_positions_without_exported_record": high - low + 1 - rows,
                            "first_record_at": start, "last_record_at": end,
                            "source_sha256": fingerprint if isinstance(fingerprint, str)
                            and len(fingerprint) == 64 and all(c in "0123456789abcdef" for c in fingerprint) else None})
    return {"records": count, "types": kinds, "first_record_at": first, "last_record_at": last,
            "session_count": total, "sessions": preview, "sessions_truncated": total > len(preview),
            "session_scope": "Preview only; complete session metadata is in research.jsonl",
            "sequence_gaps": "Missing positions can reflect recorder drops or the date filter"}


def spool_paper(path, stream):
    """Release the paper read snapshot before the potentially long ZIP export."""
    if path is None:
        return None
    descriptor = {"event_count": 0, "first_event_id": None, "last_event_id": None,
                  "first_event_at": None, "last_event_at": None, "journal_revision": None,
                  "state_event_seq": None, "trade_count": None, "config_revision": None}
    with read_database(path) as connection:
        row = connection.execute("SELECT state,revision FROM paper_state WHERE id=1").fetchone()
        if row:
            state = json.loads(row[0])
            descriptor.update(journal_revision=row[1], state_event_seq=state.get("event_seq"),
                              trade_count=state.get("trade_count"), config_revision=state.get("config_revision", 0),
                              state_sha256=hashlib.sha256(row[0].encode()).hexdigest())
            stream.write(encode_line({"type": "paper_account", "revision": row[1], "state": state}))
        for event_id, payload in connection.execute("SELECT event_id,payload FROM paper_events ORDER BY event_id"):
            event = json.loads(payload)
            descriptor["event_count"] += 1
            descriptor["last_event_id"] = event_id
            stamp = event.get("at")
            if descriptor["first_event_id"] is None:
                descriptor["first_event_id"] = event_id
            if isinstance(stamp, str):
                descriptor["first_event_at"] = min(descriptor["first_event_at"] or stamp, stamp)
                descriptor["last_event_at"] = max(descriptor["last_event_at"] or stamp, stamp)
            stream.write(encode_line({"type": "paper_event", "event_id": event_id, "event": event}))
    stream.seek(0)
    return descriptor


def export(research_path, paper_path, output_dir, *, day=None, part_bytes=PART_BYTES):
    """Stream records without loading the database or an entire export into RAM."""
    if day is not None:
        day = date.fromisoformat(day)
    manifest = {"format": "pulse-research-jsonl", "schema": SCHEMA, "export_id": uuid.uuid4().hex, "exported_at": iso(),
                "research_file": Path(research_path).name, "paper_file": Path(paper_path).name if paper_path else None,
                "research_date_utc": day.isoformat() if day else None,
                "paper_history_scope": "all committed events; filter by event.at when analyzing a date",
                "timestamps": "UTC ISO8601; monotonic clocks are comparable only within a session",
                "coverage": "Sampled snapshots, optional engine-step decisions and committed fill context; not raw exchange ticks",
                "decision_replay": "Full anchors resync context only; faithful replay needs uninterrupted inputs from startup; a date filter can omit required history",
                "live_settlement_index": False,
                "parts": "Send all parts with this filename prefix together; each is an independent ZIP"}
    parts = Parts(output_dir, manifest, limit=part_bytes)
    try:
        # A small streaming spool avoids holding a DELETE-mode paper journal's
        # read lock while compressing a much larger research database. The two
        # databases are separate consistent snapshots, not an atomic pair.
        with tempfile.TemporaryFile() as paper_stream, read_database(research_path) as connection:
            if connection.execute("SELECT version FROM research_meta").fetchone() != (SCHEMA,):
                raise ValueError("Unsupported research schema")
            where, parameters = "", ()
            if day:
                where = " WHERE captured_at >= ? AND captured_at < ?"
                parameters = (day.isoformat() + "T00:00:00", (day + timedelta(days=1)).isoformat() + "T00:00:00")
            research = research_coverage(connection, where, parameters)
            if not research["records"]:
                raise ValueError("No research records found for this export; check recorder status or --date")
            paper = spool_paper(paper_path, paper_stream)
            manifest.update(research_coverage=research, paper_coverage=paper,
                            snapshot_scope="Each database has a consistent read snapshot; their snapshots are independent")
            if paper:
                manifest["paper_events_after_latest_research"] = bool(
                    paper["last_event_at"] and research["last_record_at"]
                    and paper["last_event_at"] > research["last_record_at"])
            for session_id, started, ended, metadata, summary in connection.execute("SELECT * FROM sessions ORDER BY started_at"):
                parts.add({"type": "session", "session_id": session_id, "started_at": started, "ended_at": ended,
                           "metadata": json.loads(metadata), "summary": json.loads(summary) if summary else None})
            for row in connection.execute("SELECT * FROM markets ORDER BY first_seen,ticker"):
                ticker, first, last, close, metadata, result, result_at, attempts, next_attempt, error = row
                parts.add({"type": "market", "ticker": ticker, "first_seen": first, "last_seen": last,
                           "close_at": close, "metadata": json.loads(metadata), "result": result,
                           "result_at": result_at, "outcome_attempts": attempts,
                           "next_outcome_attempt": next_attempt, "outcome_error": error})
            rows = connection.execute("SELECT session_id,sequence,kind,captured_at,ticker,payload FROM records" +
                                      where + " ORDER BY captured_at,session_id,sequence", parameters)
            count = 0
            for session_id, sequence, kind, captured_at, ticker, payload in rows:
                parts.add({"type": kind, "session_id": session_id, "sequence": sequence,
                           "captured_at": captured_at, "ticker": ticker,
                           "payload": json.loads(zlib.decompress(payload))})
                count += 1
            for line in paper_stream:
                parts.add(json.loads(line))
        parts.add({"type": "export_complete", "export_id": manifest["export_id"], "research_records": count,
                   "research_coverage": research, "paper_coverage": paper, "exported_at": iso()})
        parts.complete()
        return parts.paths
    except BaseException:
        parts.close()
        for path in parts.paths:
            path.unlink(missing_ok=True)
        raise


def main():
    parser = argparse.ArgumentParser(description="Export research and paper evidence as uploadable ZIP parts")
    parser.add_argument("--research-state", type=Path, default=Path("data/research.sqlite3"))
    parser.add_argument("--paper-state", type=Path, default=Path("data/paper.sqlite3"))
    parser.add_argument("--output-dir", type=Path, default=Path("data/exports"))
    parser.add_argument("--date", help="Export research snapshots for one UTC date, YYYY-MM-DD; retain outcome labels and paper history")
    parser.add_argument("--research-only", action="store_true", help="Export without a paper journal")
    args = parser.parse_args()
    try:
        paths = export(args.research_state, None if args.research_only else args.paper_state,
                       args.output_dir, day=args.date)
    except (OSError, sqlite3.Error, ValueError, zlib.error) as error:
        parser.exit(1, f"Export failed: {error}\nNo complete export was created. Original databases were preserved.\n")
    print("Export complete. Send every ZIP part from this export:")
    for path in paths:
        print(f"  {path} ({path.stat().st_size / 1024 / 1024:.2f} MiB)")


if __name__ == "__main__":
    main()

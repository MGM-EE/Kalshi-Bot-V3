"""Export consistent read-only SQLite views into portable, bounded JSONL ZIPs.

Run after stopping the app: uv run python -m feedboard.export_research
Each part stands alone as a ZIP; send every part from an export together.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
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
        self.prefix = "research-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:6]
        self.paths = []
        self.archive = self.stream = None
        self.size = 0

    def add(self, value):
        line = (json.dumps(clean(value), allow_nan=False, separators=(",", ":")) + "\n").encode()
        if len(line) > MAX_LINE_BYTES:
            raise ValueError("A single research record exceeds the 1 MiB safety limit")
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


def export(research_path, paper_path, output_dir, *, day=None, part_bytes=PART_BYTES):
    """Stream records without loading the database or an entire export into RAM."""
    if day is not None:
        day = date.fromisoformat(day)
    manifest = {"format": "pulse-research-jsonl", "schema": SCHEMA, "exported_at": iso(),
                "research_file": Path(research_path).name, "paper_file": Path(paper_path).name if paper_path else None,
                "research_date_utc": day.isoformat() if day else None,
                "paper_history_scope": "all committed events; filter by event.at when analyzing a date",
                "timestamps": "UTC ISO8601; monotonic clocks are comparable only within a session",
                "coverage": "1-second normalized snapshots and committed fill context; not a lossless tick archive",
                "live_settlement_index": False,
                "parts": "Send all parts with this filename prefix together; each is an independent ZIP"}
    parts = Parts(output_dir, manifest, limit=part_bytes)
    try:
        with read_database(research_path) as connection:
            if connection.execute("SELECT version FROM research_meta").fetchone() != (SCHEMA,):
                raise ValueError("Unsupported research schema")
            for session_id, started, ended, metadata, summary in connection.execute("SELECT * FROM sessions ORDER BY started_at"):
                parts.add({"type": "session", "session_id": session_id, "started_at": started, "ended_at": ended,
                           "metadata": json.loads(metadata), "summary": json.loads(summary) if summary else None})
            for row in connection.execute("SELECT * FROM markets ORDER BY first_seen,ticker"):
                ticker, first, last, close, metadata, result, result_at, attempts, next_attempt, error = row
                parts.add({"type": "market", "ticker": ticker, "first_seen": first, "last_seen": last,
                           "close_at": close, "metadata": json.loads(metadata), "result": result,
                           "result_at": result_at, "outcome_attempts": attempts,
                           "next_outcome_attempt": next_attempt, "outcome_error": error})
            where, parameters = "", ()
            if day:
                where = " WHERE captured_at >= ? AND captured_at < ?"
                parameters = (day.isoformat() + "T00:00:00", (day + timedelta(days=1)).isoformat() + "T00:00:00")
            rows = connection.execute("SELECT session_id,sequence,kind,captured_at,ticker,payload FROM records" +
                                      where + " ORDER BY captured_at,session_id,sequence", parameters)
            count = 0
            for session_id, sequence, kind, captured_at, ticker, payload in rows:
                parts.add({"type": kind, "session_id": session_id, "sequence": sequence,
                           "captured_at": captured_at, "ticker": ticker,
                           "payload": json.loads(zlib.decompress(payload))})
                count += 1
            if count == 0:
                raise ValueError("No research records found for this export; check recorder status or --date")
        if paper_path is not None:
            with read_database(paper_path) as connection:
                row = connection.execute("SELECT state,revision FROM paper_state WHERE id=1").fetchone()
                if row:
                    parts.add({"type": "paper_account", "revision": row[1], "state": json.loads(row[0])})
                for event_id, payload in connection.execute("SELECT event_id,payload FROM paper_events ORDER BY event_id"):
                    parts.add({"type": "paper_event", "event_id": event_id, "event": json.loads(payload)})
        parts.add({"type": "export_complete", "research_records": count, "exported_at": iso()})
        parts.close()
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

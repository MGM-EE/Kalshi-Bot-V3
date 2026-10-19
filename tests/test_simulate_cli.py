"""Exercise the user commands, reproducible reports, and zero-network mode."""
import contextlib
import csv
import io
import json
import tempfile
import unittest
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from feedboard.historical import HistoryCache
from feedboard.simulate import configurations, date_range, main, parser

START = int(datetime(2026, 5, 1, tzinfo=timezone.utc).timestamp())
END = START + 86400


def iso(stamp):
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat()


class SimulationCommandTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.cache = HistoryCache(self.root / "history.sqlite3")
        self.cache.initialize()
        self.raw = [{"ticker": f"KXBTC15M-CLI-{index}", "close_time": iso(START + index * 900),
                     "settlement_ts": iso(START + index * 900 + .5),
                     "status": "finalized", "result": "yes" if index <= 8 else "no",
                     "market_type": "binary", "strike_type": "greater_or_equal", "floor_strike": 100000}
                    for index in range(1, 13)]
        self.cache.catalog(START, END, [(raw, False) for raw in self.raw], {"cutoff": {}})
        for index, raw in enumerate(self.raw):
            rows = [{"end_period_ts": START + index * 900 + (minute + 1) * 60,
                     "yes_bid": {"open_dollars": ".81" if minute == 6 else ".29"},
                     "yes_ask": {"open_dollars": ".82" if minute == 6 else ".30"}}
                    for minute in range(15)]
            self.cache.save_candles(raw, rows, False)
        self.output = self.root / "reports"
        self.arguments = ["simulate", "--offline", "--cache", str(self.cache.path),
                          "--output-dir", str(self.output), "--min-train-trades", "1"]

    def invoke(self, *arguments):
        output, errors = io.StringIO(), io.StringIO()
        with patch("sys.argv", [*self.arguments, *arguments]), \
                patch("feedboard.simulate.HistoryDownloader.download", new_callable=AsyncMock) as download, \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            main()
        download.assert_not_called()
        self.assertEqual(errors.getvalue(), "")
        return output.getvalue()

    def test_default_grid_offline_reports_and_exported_grid_reproduce(self):
        output = self.invoke()
        self.assertIn("270 configurations across 12 markets", output)
        first = next(self.output.glob("*.zip"))
        with zipfile.ZipFile(first) as archive:
            self.assertIsNone(archive.testzip())
            self.assertEqual(set(archive.namelist()),
                             {"summary.json", "grid.json", "leaderboard.csv", "selected_trades.csv", "READ_ME.txt"})
            summary = json.loads(archive.read("summary.json"))
            rows = list(csv.DictReader(io.StringIO(archive.read("leaderboard.csv").decode())))
            trades = list(csv.DictReader(io.StringIO(archive.read("selected_trades.csv").decode())))
        self.assertEqual(summary["configurations"], 270)
        self.assertEqual(len(rows), 270)
        self.assertEqual(summary["dataset"]["start_ts"], START)
        self.assertEqual(summary["dataset"]["end_ts"], END)
        self.assertEqual(summary["dataset"]["expected_calendar_rounds"], 96)
        self.assertEqual(summary["dataset"]["observed_close_slots"], 12)
        self.assertEqual(summary["dataset"]["missing_minutes"], 0)
        self.assertEqual(len(summary["dataset"]["sha256"]), 64)
        self.assertEqual(len(summary["source_sha256"]), 64)
        self.assertEqual(summary["split"]["train_fraction"], .7)
        self.assertEqual(summary["split"]["train_markets"], 8)
        self.assertEqual(summary["split"]["test_markets"], 3)
        self.assertEqual(len(summary["split"]["purged_boundary_tickers"]), 1)
        selected = summary["selected_using_training_only"]
        self.assertGreater(selected["train"]["net_pnl"], 0)
        self.assertLess(selected["test"]["net_pnl"], 0)
        self.assertEqual(len(trades), 11)
        for split in ("train", "test"):
            self.assertAlmostEqual(sum(float(t["pnl"]) for t in trades if t["split"] == split), selected[split]["net_pnl"])
        self.assertEqual(rows[0]["config_id"], selected["config_id"])
        self.invoke("--grid", str(first.with_suffix("") / "grid.json"))
        second = next(path for path in self.output.glob("*.zip") if path != first)
        with zipfile.ZipFile(second) as archive:
            replayed = json.loads(archive.read("summary.json"))
        self.assertEqual(summary["selected_using_training_only"], replayed["selected_using_training_only"])
        self.assertEqual(summary["dataset"]["sha256"], replayed["dataset"]["sha256"])

    def test_fetch_only_and_no_qualifying_config_have_clear_outputs(self):
        self.assertIn("Cached in", self.invoke("--fetch-only"))
        self.assertFalse(self.output.exists())
        self.assertIn("no winner selected", self.invoke("--min-train-trades", "100"))
        with zipfile.ZipFile(next(self.output.glob("*.zip"))) as archive:
            summary = json.loads(archive.read("summary.json"))
            self.assertIsNone(summary["selected_using_training_only"])
            self.assertEqual(len(archive.read("selected_trades.csv").splitlines()), 1)

    def test_invalid_inputs_stop_before_reports_or_network(self):
        grid = self.root / "bad-grid.json"
        grid.write_text('[{"unknown_rule": true}]', encoding="utf-8")
        for flags in (("--refresh",), ("--cash", "nan"), ("--days", "0"),
                      ("--train-fraction", "1"), ("--buy-min", "90", "--buy-max", "20"),
                      ("--start", "2026-02-30"), ("--grid", str(grid))):
            with self.subTest(flags=flags), patch("sys.argv", [*self.arguments, *flags]), \
                    patch("feedboard.simulate.HistoryDownloader.download", new_callable=AsyncMock) as download, \
                    contextlib.redirect_stderr(io.StringIO()) as errors, self.assertRaises(SystemExit) as stopped:
                main()
            self.assertEqual(stopped.exception.code, 1)
            self.assertIn("Simulation stopped:", errors.getvalue())
            download.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_missing_cache_or_all_missing_quotes_do_not_report_fake_results(self):
        with patch("sys.argv", [*self.arguments, "--cache", str(self.root / "absent.sqlite3")]), \
                contextlib.redirect_stderr(io.StringIO()) as errors, self.assertRaises(SystemExit):
            main()
        self.assertIn("run online once first", errors.getvalue())
        self.assertFalse((self.root / "absent.sqlite3").exists())
        for raw in self.raw:
            self.cache.save_candles(raw, [], False)
        with patch("sys.argv", self.arguments), contextlib.redirect_stderr(io.StringIO()) as errors, \
                self.assertRaises(SystemExit):
            main()
        self.assertIn("No usable bid/ask quote history", errors.getvalue())
        self.assertFalse(self.output.exists())

    def test_completed_utc_close_dates_and_default_cached_range(self):
        arguments = parser()
        self.assertEqual(date_range(arguments.parse_args(["--offline"]), self.cache), (START, END))
        self.assertEqual(date_range(arguments.parse_args(["--offline", "--start", "2026-05-01", "--end", "2026-05-02"]),
                                    self.cache), (START, END))
        self.assertEqual(len(configurations(arguments.parse_args([]))), 270)

"""Settings callers must finish even when shutdown interrupts a disk write."""
import asyncio
import tempfile
import threading
import unittest
from pathlib import Path

from feedboard.core import Settings, Store
from feedboard.paper import PaperConfig, PaperEngine
from feedboard.paper_runtime import PaperJournal, SettingsUnavailable, run_paper


class SettingsShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def test_shutdown_rejects_active_request_without_publishing_unconfirmed_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "paper.sqlite3"
            original = PaperEngine().dump_state()
            journal = PaperJournal(path)
            journal.save(original, [], 0)
            store = Store(Settings())
            loop = asyncio.get_running_loop()
            started, finished = asyncio.Event(), asyncio.Event()
            release = threading.Event()
            worker_errors = []

            class BlockingSettingsJournal(PaperJournal):
                def save(self, state, events, revision):
                    loop.call_soon_threadsafe(started.set)
                    try:
                        if not release.wait(5):
                            raise TimeoutError("test did not release settings write")
                        return super().save(state, events, revision)
                    except BaseException as error:
                        worker_errors.append(error)
                        raise
                    finally:
                        loop.call_soon_threadsafe(finished.set)

            runner = asyncio.create_task(run_paper(store, BlockingSettingsJournal(path), PaperConfig()))
            caller = None
            try:
                async with asyncio.timeout(3):
                    while store.paper_control is None or not store.paper_control.available:
                        await asyncio.sleep(.01)
                caller = asyncio.create_task(store.paper_control.change({"contracts": 7}, 0))
                await asyncio.wait_for(started.wait(), 3)

                # The draft is on a worker thread. Neither API settings nor the
                # running account may publish it before the transaction finishes.
                self.assertEqual(store.paper_control.document()["settings"]["contracts"], 5)
                self.assertEqual(store.paper_engine.config.contracts, 5)
                self.assertEqual(await asyncio.to_thread(journal.load), (original, 1))

                runner.cancel()
                await asyncio.wait_for(asyncio.gather(runner, return_exceptions=True), 1)
                with self.assertRaisesRegex(SettingsUnavailable, "stopped during a settings save"):
                    await asyncio.wait_for(asyncio.shield(caller), 1)
                self.assertFalse(release.is_set())
                self.assertFalse(finished.is_set())
                self.assertFalse(store.paper_control.available)
                self.assertEqual(store.paper_engine.config.contracts, 5)
            finally:
                # Cancelling to_thread cannot stop a write already executing.
                # Always release and join it before deleting the database.
                release.set()
                runner.cancel()
                await asyncio.gather(runner, return_exceptions=True)
                if started.is_set():
                    await asyncio.wait_for(finished.wait(), 3)
                if caller is not None:
                    caller.cancel()
                    await asyncio.gather(caller, return_exceptions=True)

            self.assertEqual(worker_errors, [])
            actual, journal_revision = await asyncio.to_thread(journal.load)
            self.assertEqual(journal_revision, 2)
            self.assertEqual(actual["config"]["contracts"], 7)
            self.assertEqual(actual["config_revision"], 1)
            self.assertEqual(actual["cash"], original["cash"])
            self.assertEqual(actual["events"], [])
            self.assertEqual(store.paper_engine.config.contracts, 5)


if __name__ == "__main__":
    unittest.main()

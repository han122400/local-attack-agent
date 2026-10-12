"""Failure gates for the durable run finalizer (mock lab, real disk runtime)."""

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from command_runtime import CommandRuntime
from explorer import ExplorerV3
from session_log import LogFailure, SessionStore


RUN_ID = "e" * 32


class FinalizerFailureTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.probes = []
        self.explorer = ExplorerV3(self.root, browser_enabled=False, lab_reset=self._reset)
        self.store = SessionStore(self.root)
        self.log = self.store.create(RUN_ID, {"goal": "inspect", "target": "http://127.0.0.1:3000/",
                                                   "managed_processes": [], "runtime_creation_intent": False})
        self.runtime = CommandRuntime(self.root, self.log)
        self.runtime.start_session(RUN_ID, browser=False)
        self.runtime.run(RUN_ID, {"action": "write_file", "path": "work/one.txt", "content": "evidence"})

    def _reset(self):
        self.probes.append("reset")
        return {"verified": True}

    def _assert_blocked(self):
        self.assertFalse(self.explorer.can_start()[0])
        with self.assertRaises(RuntimeError):
            self.explorer.start(goal="second")

    def _recover_after_restart(self):
        restarted = ExplorerV3(self.root, browser_enabled=False, lab_reset=self._reset)
        self.assertFalse(restarted.can_start()[0])
        self.assertTrue(restarted.recover(RUN_ID))
        restarted.worker.join(10)
        self.assertFalse(restarted.worker.is_alive())
        self.assertEqual(self.store.read(RUN_ID)["phase"], "finished")
        self.assertFalse((self.root / RUN_ID / "runtime").exists())
        self.assertTrue(restarted.can_start()[0])
        self.assertFalse(restarted.recover(RUN_ID))

    def test_stop_failure_keeps_runtime_and_does_not_reset_lab(self):
        with patch.object(self.runtime, "stop", side_effect=RuntimeError("stop failed")):
            self.explorer._finalize(RUN_ID, self.log, self.runtime, "completed", "done")
        self.assertEqual(self.probes, [])
        self.assertTrue(self.runtime.runtime_dir.exists())
        self.assertEqual(self.store.read(RUN_ID)["cleanup_status"], "failed")
        self._assert_blocked()
        self._recover_after_restart()

    def test_evidence_failure_keeps_runtime_and_blocks_next_start(self):
        with patch.object(self.runtime, "export_evidence", side_effect=LogFailure("copy failed")):
            self.explorer._finalize(RUN_ID, self.log, self.runtime, "error", "model failed")
        self.assertEqual(self.probes, [])
        self.assertEqual(self.store.read(RUN_ID)["run_result"], "error")
        self.assertTrue(self.runtime.runtime_dir.exists())
        self._assert_blocked()
        self._recover_after_restart()

    def test_destroy_failure_never_resets_lab(self):
        with patch.object(self.runtime, "destroy", side_effect=OSError("delete failed")):
            self.explorer._finalize(RUN_ID, self.log, self.runtime, "completed", "done")
        self.assertEqual(self.probes, [])
        self.assertTrue(self.runtime.runtime_dir.exists())
        self._assert_blocked()
        self._recover_after_restart()

    def test_final_log_flush_failure_cannot_mark_finished(self):
        original_flush = self.log.flush
        calls = 0
        def fail_final_flush():
            nonlocal calls
            calls += 1
            if calls == 2:
                raise LogFailure("final flush failed")
            return original_flush()
        with patch.object(self.log, "flush", side_effect=fail_final_flush):
            self.explorer._finalize(RUN_ID, self.log, self.runtime, "completed", "done")
        self.assertEqual(self.probes, ["reset"])
        self.assertNotEqual(self.store.read(RUN_ID)["phase"], "finished")
        self._assert_blocked()
        self._recover_after_restart()

    def test_writer_failure_still_stops_process_and_preserves_runtime(self):
        original_update = self.log.update
        first = True
        def fail_first_update(**fields):
            nonlocal first
            if first:
                first = False
                self.log.failed = True
                raise LogFailure("metadata disk failure")
            return original_update(**fields)
        with patch.object(self.log, "update", side_effect=fail_first_update):
            self.explorer._finalize(RUN_ID, self.log, self.runtime, "error", "log failed")
        self.assertEqual(self.probes, [])
        self.assertTrue(self.runtime.runtime_dir.exists())
        self.assertFalse(self.runtime.inspect(RUN_ID)["managed_processes"])
        self._assert_blocked()

    def test_reset_failure_retains_result_and_retries_only_reset(self):
        self.explorer.lab_reset = lambda: (_ for _ in ()).throw(RuntimeError("probe failed"))
        self.explorer._finalize(RUN_ID, self.log, self.runtime, "timed_out", "budget")
        state = self.store.read(RUN_ID)
        self.assertEqual(state["run_result"], "timed_out")
        self.assertEqual(state["cleanup_status"], "succeeded")
        self.assertFalse(self.runtime.runtime_dir.exists())
        self._assert_blocked()
        self.explorer.lab_reset = self._reset
        self.assertTrue(self.explorer.recover(RUN_ID))
        self.explorer.worker.join(10)
        self.assertFalse(self.explorer.worker.is_alive())
        self.assertEqual(self.store.read(RUN_ID)["run_result"], "timed_out")
        self.assertTrue(self.explorer.can_start()[0])

    def test_finalize_budget_exhaustion_stays_recoverable(self):
        self.log.update(budgets={"finalize_seconds": 1})
        def slow_reset():
            time.sleep(1.1)
            return {"verified": True}
        self.explorer.lab_reset = slow_reset
        self.explorer._finalize(RUN_ID, self.log, self.runtime, "completed", "done")
        state = self.store.read(RUN_ID)
        self.assertEqual(state["run_result"], "completed")
        self.assertEqual(state["phase"], "recovery_required")
        self.assertFalse(self.explorer.can_start()[0])
        self._recover_after_restart()


if __name__ == "__main__":
    unittest.main()

import json
import tempfile
import threading
import unittest
from pathlib import Path

from session_log import SessionLog, LogFailure, SessionStore, ControllerLease


RUN_ID = "a" * 32


class SessionLogTests(unittest.TestCase):
    def test_events_are_append_only_ordered_and_run_scoped(self):
        with tempfile.TemporaryDirectory() as temp:
            log = SessionLog(Path(temp), RUN_ID, {"goal": "inspect", "run_result": None})
            workers = [threading.Thread(target=lambda: [log.append("output", {"text": "x"}) for _ in range(10)])
                       for _ in range(4)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join()
            events = log.read_events()
            self.assertEqual([event["seq"] for event in events], list(range(1, 41)))
            self.assertEqual({event["run_id"] for event in events}, {RUN_ID})
            self.assertEqual(len(events), 40)
            log.close()
            with self.assertRaises(LogFailure):
                log.append("late", {})

    def test_metadata_updates_are_atomic_and_artifacts_keep_prior_versions(self):
        with tempfile.TemporaryDirectory() as temp:
            log = SessionLog(Path(temp), RUN_ID, {"goal": "inspect", "log_status": "collecting"})
            log.update(log_status="preserved", cleanup_status="pending")
            first = log.preserve_bytes("code/a.py", b"print(1)")
            second = log.preserve_bytes("code/a.py", b"print(2)")
            self.assertNotEqual(first["path"], second["path"])
            self.assertEqual((log.run_dir / first["path"]).read_bytes(), b"print(1)")
            self.assertEqual((log.run_dir / second["path"]).read_bytes(), b"print(2)")
            self.assertEqual(first["bytes"], 8)
            self.assertEqual(len(first["sha256"]), 64)
            self.assertEqual(json.loads((log.run_dir / "session.json").read_text(encoding="utf-8"))["cleanup_status"], "pending")
            self.assertFalse(list(log.run_dir.glob("*.tmp")))
            log.close()

    def test_artifact_path_escape_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            log = SessionLog(Path(temp), RUN_ID, {})
            for path in ("../other", "/absolute", "..\\other", "C:\\escape"):
                with self.subTest(path=path), self.assertRaises(ValueError):
                    log.preserve_bytes(path, b"x")
            log.close()

    def test_incomplete_session_blocks_start_after_store_restart(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = SessionStore(root)
            log = store.create(RUN_ID, {"goal": "inspect"})
            log.close()
            self.assertFalse(SessionStore(root).can_start()[0])
            reopened = SessionLog(root, RUN_ID, recovery=True)
            reopened.update(run_result="completed", log_status="finalized", cleanup_status="succeeded",
                            lab_reset_status="succeeded", phase="finished")
            reopened.close()
            self.assertTrue(SessionStore(root).can_start()[0])
            (root / ("b" * 32) / "runtime").mkdir(parents=True)
            self.assertFalse(SessionStore(root).can_start()[0])

    def test_controller_lease_excludes_second_controller(self):
        with tempfile.TemporaryDirectory() as temp:
            first = ControllerLease(Path(temp))
            self.assertTrue(first.acquire())
            second = ControllerLease(Path(temp))
            self.assertFalse(second.acquire())
            first.release()
            self.assertTrue(second.acquire())
            second.release()

    def test_recovery_keeps_monotonic_sequence_past_first_page_and_preserves_torn_tail(self):
        with tempfile.TemporaryDirectory() as temp:
            log = SessionLog(Path(temp), RUN_ID, {})
            log.close()
            with log.events_path.open("w", encoding="utf-8") as stream:
                for seq in range(1, 1002):
                    stream.write(json.dumps({"run_id": RUN_ID, "seq": seq, "kind": "test", "at": 1}) + "\n")
                stream.write('{"partial":')
            recovered = SessionLog(Path(temp), RUN_ID, recovery=True)
            event = recovered.append("after_recovery", {})
            self.assertGreater(event["seq"], 1001)
            self.assertTrue(any(path.read_bytes() == b'{"partial":' for path in recovered.artifacts_dir.rglob("*partial*.bin") if path.is_file()))
            self.assertEqual(len(recovered.read_events(after_seq=1000)), event["seq"] - 1000)
            recovered.close()


if __name__ == "__main__":
    unittest.main()

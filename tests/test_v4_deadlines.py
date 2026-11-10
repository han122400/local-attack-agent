import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from engine.harness import Harness
from engine.lab import reset_lab
from engine.session_log import SessionLog


class DeadlineTests(unittest.TestCase):
    def test_default_model_transport_receives_remaining_deadline(self):
        with tempfile.TemporaryDirectory() as temp:
            log = SessionLog(Path(temp), "a" * 32)
            self.addCleanup(log.close)
            command = {"action": "finish", "plan": "done", "reason": "done", "path": "", "content": "", "code": "", "command": ""}
            def stream(payload, *, timeout_seconds=None):
                self.assertIsNotNone(timeout_seconds)
                self.assertGreater(timeout_seconds, 0)
                self.assertLessEqual(timeout_seconds, .25)
                yield {"message": {"content": json.dumps(command)}}
            with patch("engine.harness._model_stream", side_effect=stream):
                Harness(log).decide(log.run_id, {}, model="mock:model", settings={"think": False, "options": {"num_ctx": 8192, "num_predict": 4096}},
                                    deadline=time.monotonic() + .25)

    def test_expired_reset_deadline_does_not_issue_docker_command(self):
        with patch("engine.lab._docker", side_effect=AssertionError("No Docker after deadline")):
            with self.assertRaises((TimeoutError, RuntimeError)):
                reset_lab(deadline=time.monotonic() - 1)


if __name__ == "__main__":
    unittest.main()

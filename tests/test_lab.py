import json
import subprocess
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import app
import lab


def inspected(status):
    return subprocess.CompletedProcess([], 0, json.dumps({"Status": status}), "")


class LabTests(unittest.TestCase):
    def test_running_requires_web_response_to_be_ready(self):
        with patch("lab._docker", return_value=inspected("running")), patch("lab._web_ready", return_value=False):
            self.assertEqual(lab.get_status()["status"], "starting")
        with patch("lab._docker", return_value=inspected("running")), patch("lab._web_ready", return_value=True):
            state = lab.get_status()
            self.assertTrue(state["ready"])
            self.assertFalse(state["can_start"])

    def test_stopped_container_can_be_started(self):
        with patch("lab._docker", return_value=inspected("exited")), patch("lab._web_ready") as probe:
            state = lab.get_status()
            self.assertEqual(state["status"], "stopped")
            self.assertTrue(state["can_start"])
            probe.assert_not_called()

    def test_missing_container_and_unavailable_docker_are_distinct(self):
        for stderr, status in [("Error: No such object: juice-lab", "missing"), ("cannot connect to daemon", "unavailable")]:
            with self.subTest(status=status), patch("lab._docker", return_value=subprocess.CompletedProcess([], 1, "", stderr)):
                state = lab.get_status()
                self.assertEqual(state["status"], status)
                self.assertFalse(state["can_start"])
        with patch("lab._docker", side_effect=FileNotFoundError):
            self.assertEqual(lab.get_status()["status"], "unavailable")

    def test_start_only_starts_fixed_existing_container(self):
        results = [inspected("exited"), subprocess.CompletedProcess([], 0, "juice-lab", ""), inspected("running")]
        with patch("lab._docker", side_effect=results) as docker, patch("lab._web_ready", return_value=False):
            self.assertEqual(lab.start_lab()["status"], "starting")
            self.assertEqual(docker.call_args_list[1].args, ("start", "juice-lab"))

    def test_already_running_is_not_started_again(self):
        with patch("lab._docker", return_value=inspected("running")) as docker, patch("lab._web_ready", return_value=True):
            self.assertTrue(lab.start_lab()["ready"])
            self.assertEqual(docker.call_count, 1)

    def test_start_failure_is_reported(self):
        for result in [subprocess.CompletedProcess([], 1, "", "port conflict"), subprocess.TimeoutExpired("docker", 20)]:
            with self.subTest(result=result), patch("lab.get_status", return_value={"status": "stopped", "can_start": True}), patch("lab._docker", side_effect=[result]):
                with self.assertRaises(RuntimeError):
                    lab.start_lab()


class LabRouteTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app.app, base_url="http://127.0.0.1:8765")

    def test_status_route_and_local_start(self):
        state = {"status": "stopped", "can_start": True}
        with patch("app.get_lab_status", return_value=state):
            response = self.client.get("/api/lab")
            self.assertEqual(response.json(), state)
            self.assertEqual(response.headers["cache-control"], "no-store")
        with patch("app.start_lab", return_value={"status": "starting"}) as start:
            response = self.client.post("/api/lab/start", headers={"Origin": "http://127.0.0.1:8765"})
            self.assertEqual(response.status_code, 200)
            start.assert_called_once_with()

    def test_cross_site_start_is_blocked_and_errors_are_returned(self):
        with patch("app.start_lab") as start:
            response = self.client.post("/api/lab/start", headers={"Origin": "https://example.com"})
            self.assertEqual(response.status_code, 403)
            start.assert_not_called()
        with patch("app.start_lab", side_effect=RuntimeError("Docker 연결 안 됨")):
            response = self.client.post("/api/lab/start")
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.json()["detail"], "Docker 연결 안 됨")


if __name__ == "__main__":
    unittest.main()

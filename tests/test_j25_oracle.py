import json
import subprocess
import unittest
from unittest.mock import patch

from j25_oracle import capture_j25_lab_oracle, evaluate_j25_lab_run


class OracleTests(unittest.TestCase):
    def test_capture_strips_challenge_details_and_secrets(self):
        rows = {"status": "success", "data": [
            {"id": 1, "solved": True, "name": "secret challenge", "description": "token=private"},
            {"id": 2, "solved": False, "key": "hidden"}]}
        class Response:
            status = 200
            def __enter__(self):
                return self
            def __exit__(self, *_):
                pass
            def read(self):
                return json.dumps(rows).encode()
        info = [{"Config": {"Image": "bkimminich/juice-shop:v20.2.0"}}]
        with patch("lab.urlopen", return_value=Response()), patch("lab._docker",
            return_value=subprocess.CompletedProcess([], 0, json.dumps(info), "")):
            result = capture_j25_lab_oracle("http://127.0.0.1:3000/")
        self.assertEqual(result["solved_ids"], [1])
        self.assertEqual(result["challenge_count"], 2)
        self.assertNotIn("secret challenge", json.dumps(result))
        self.assertNotIn("token=private", json.dumps(result))

    def test_oracle_requires_information_proof_and_solved_link(self):
        ledger = {"cases": [
            {"id": "a", "status": "independently_verified", "information": ["test-note"],
             "proofs": [{"body_sha256": "a" * 64}], "oracle_challenge_id": 1},
            {"id": "b", "status": "independently_verified", "information": [],
             "proofs": [], "oracle_challenge_id": 2},
            {"id": "c", "status": "attempted", "information": ["test-note"],
             "proofs": [{"body_sha256": "b" * 64}], "oracle_challenge_id": 1},
            {"id": "d", "status": "independently_verified", "information": ["test-note"],
             "proofs": [{"body_sha256": "c" * 64}]}]}
        result = evaluate_j25_lab_run(ledger, {"status": "available", "solved_ids": [1, 2]})
        self.assertEqual(result["confirmed_case_ids"], ["a"])
        self.assertEqual(result["confirmed_count"], 1)
        self.assertEqual(result["unlinked_case_ids"], ["d"])
        self.assertEqual(evaluate_j25_lab_run(ledger, {"status": "unavailable"})["confirmed_count"], 0)

    def test_rejects_non_lab_target(self):
        with self.assertRaises(ValueError):
            capture_j25_lab_oracle("https://fixture.invalid/")


if __name__ == "__main__":
    unittest.main()

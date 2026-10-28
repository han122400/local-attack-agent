import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import httpx2 as httpx

from session_log import SessionLog
from v4_oracle import LabOracle
from v4_policy import oracle_verdict
from test_v4_policy import NODE, proof


class OracleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.log = SessionLog(Path(self.temp.name), "a" * 32, {"experiment_mode": "v4", "target": "http://127.0.0.1:3000/",
            "initial_lab_status": "succeeded", "initial_lab_probe": {"verified": True, "image": "bkimminich/juice-shop:v20.2.0"}})
        self.addCleanup(self.log.close)
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.execute("CREATE TABLE Products(id,name,description,price,deluxePrice,image,createdAt,updatedAt,deletedAt)")
        self.solved, self.patched, self.requests = False, False, []
        def handler(request):
            self.requests.append(request)
            if request.url.path == "/api/Challenges/":
                return httpx.Response(200, json={"status": "success", "data": [{**proof()["before"], "solved": self.solved}]})
            query = request.url.params.get("q", "")
            if self.patched:
                query = query.replace("'", "''")
            sql = f"SELECT * FROM Products WHERE ((name LIKE '%{query}%' OR description LIKE '%{query}%') AND deletedAt IS NULL) ORDER BY name"
            cursor = self.db.execute(sql)
            rows = [dict(zip([item[0] for item in cursor.description], row)) for row in cursor.fetchall()]
            # The pinned search route localizes both fields after SQL; keep
            # the canary compatible with those application-level string inputs.
            if any(not isinstance(row["name"], str) or not isinstance(row["description"], str) for row in rows):
                return httpx.Response(500, json={"error": "invalid localization input"})
            return httpx.Response(200, json={"status": "success", "data": rows})
        client = httpx.Client(transport=httpx.MockTransport(handler))
        self.addCleanup(client.close)
        self.oracle = LabOracle(self.log, {"exclusive_lab": True}, client=client,
                                identity_reader=lambda: proof()["identity_before"])
        self.attempt = {"attempt_id": "A0001", "node_id": "N0002", "exec_id": "EXEC-0001"}

    def test_collector_runs_real_sql_semantics_and_preserves_independent_proof(self):
        token = self.oracle.begin(self.attempt, NODE)
        self.solved = True
        observed, refs = self.oracle.finish(token, self.attempt, NODE)
        self.assertEqual(oracle_verdict(observed, NODE, execution_status="succeeded")["verdict"], "confirmed")
        self.assertEqual(len(refs), 5)
        for ref in refs:
            self.assertTrue((self.log.run_dir / ref["path"]).is_file())
            self.assertEqual(ref["source"], "harness_http")
        for request in self.requests[2:]:
            self.assertNotIn("Authorization", request.headers)

    def test_solved_flag_with_parameterized_behavior_is_inconclusive(self):
        token = self.oracle.begin(self.attempt, NODE)
        self.solved, self.patched = True, True
        observed, refs = self.oracle.finish(token, self.attempt, NODE)
        self.assertEqual(oracle_verdict(observed, NODE, execution_status="succeeded")["verdict"], "inconclusive")

    def test_no_isolation_or_pre_solved_lab_does_not_run_test_queries(self):
        self.solved = True
        self.assertIsNone(self.oracle.begin(self.attempt, NODE))
        self.assertEqual(len(self.requests), 1)

    def test_wrong_lab_identity_prevents_any_oracle_request(self):
        self.oracle.identity_reader = lambda: {**proof()["identity_before"], "image": "other:version"}
        self.assertIsNone(self.oracle.begin(self.attempt, NODE))
        self.assertFalse(self.requests)

    def test_before_snapshot_cannot_be_rebound_to_another_attempt(self):
        token = self.oracle.begin(self.attempt, NODE)
        self.solved = True
        observed, refs = self.oracle.finish(token, {**self.attempt, "attempt_id": "A0002"}, NODE)
        self.assertIsNone(observed)
        self.assertFalse(refs)
        self.oracle.settings["exclusive_lab"] = False
        self.assertIsNone(self.oracle.begin(self.attempt, NODE))
        self.assertEqual(len(self.requests), 1)


if __name__ == "__main__":
    unittest.main()

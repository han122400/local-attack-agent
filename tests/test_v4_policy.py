import copy
import unittest

from v4_policy import execution_settings, oracle_verdict, POLICY_VERSION


def proof():
    return {"policy_version": POLICY_VERSION, "source": "harness_http", "run_id": "a" * 32,
            "node_id": "N0002", "attempt_id": "A0001", "exec_id": "EXEC-0001",
            "target": "http://127.0.0.1:3000/", "exclusive_lab": True, "fresh_reset": True,
            "before": {"key": "dbSchemaChallenge", "name": "Database Schema", "category": "Injection", "id": 20, "solved": False},
            "after": {"key": "dbSchemaChallenge", "name": "Database Schema", "category": "Injection", "id": 20, "solved": True},
            "identity_before": {"image": "bkimminich/juice-shop:v20.2.0", "container_id": "fixed", "image_id": "sha256:fixed", "started_at": "epoch"},
            "identity_after": {"image": "bkimminich/juice-shop:v20.2.0", "container_id": "fixed", "image_id": "sha256:fixed", "started_at": "epoch"},
            "canary": {"control_absent": True, "test_present": True, "repeat_present": True},
            "evidence_count": 5}


NODE = {"kind": "candidate", "hypothesis": {"type": "sqli"},
        "locator": {"url": "http://127.0.0.1:3000/rest/products/search", "method": "GET", "input": "q"}}


class PolicyTests(unittest.TestCase):
    def test_known_lab_positive_can_be_confirmed(self):
        self.assertEqual(oracle_verdict(proof(), NODE, execution_status="succeeded")["verdict"], "confirmed")

    def test_solved_claims_and_differences_alone_never_confirm(self):
        changes = [{"exclusive_lab": False}, {"fresh_reset": False}, {"source": "model_file"},
                   {"canary": {"control_absent": True, "test_present": False, "repeat_present": False}},
                   {"evidence_count": 0}, {"identity_after": {"image": "other"}},
                   {"before": {**proof()["before"], "solved": True}},
                   {"after": {**proof()["after"], "id": 999}}, {"policy_version": "other"}]
        for change in changes:
            with self.subTest(change=change):
                self.assertEqual(oracle_verdict({**proof(), **change}, NODE, execution_status="succeeded")["verdict"], "inconclusive")
        self.assertEqual(oracle_verdict(proof(), NODE, execution_status="failed")["verdict"], "inconclusive")

    def test_scope_is_exact_and_other_vulnerabilities_are_inconclusive(self):
        for changes in ({"hypothesis": {"type": "xss"}}, {"kind": "surface"},
                        {"locator": {**NODE["locator"], "url": "https://other.test/rest/products/search"}}):
            self.assertEqual(oracle_verdict(proof(), {**NODE, **changes}, execution_status="succeeded")["verdict"], "inconclusive")

    def test_settings_are_versioned_and_reject_unknown_or_bad_values(self):
        settings = execution_settings({})
        self.assertEqual(settings["version"], "v4-execution-v1")
        self.assertFalse(settings["exclusive_lab"])
        for value in ({"exclusive_lab": "yes"}, {"node_attempts": 0}, {"max_cost_usd": float("nan")}, {"api_key": "secret"}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                execution_settings(value)


if __name__ == "__main__":
    unittest.main()

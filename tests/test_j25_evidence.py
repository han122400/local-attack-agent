import json
import tempfile
import unittest
from pathlib import Path

from j25_evidence import reduce_j25_evidence
from scorecard import score_run
from session_log import SessionLog, SessionStore


RUN_ID = "9" * 32


def step(number, status="independently_verified", *, url="https://fixture.invalid/search?token=private",
         action="verify", reason="private_canary_observed"):
    return {"step": number, "exec_id": f"EXEC-{number}", "action": action, "ref": f"I{number}",
            "identity": "public", "verification_kind": "injection", "status": "ok",
            "reference": {"url": url, "field": "q", "method": "GET", "basis": "observed"},
            "test": "CANARY-RAW-SECRET", "hypothesis": "untrusted model claim",
            "comparison": {"samples": [{"stage": "normal", "status": 200, "body_sha256": "a"},
                {"stage": "test", "status": 200, "body_sha256": "b", "canary_hits": ["private-note"]},
                {"stage": "recheck", "status": 200, "body_sha256": "a"}]},
            "verification": {"kind": "injection", "status": status,
                             "information": ["private-note"] if status == "independently_verified" else [],
                             "reason": reason}}


class J25EvidenceTests(unittest.TestCase):
    def test_duplicate_attempts_on_one_surface_make_one_verified_case(self):
        ledger = reduce_j25_evidence([step(1), step(2)], {"inputs": []})
        self.assertEqual(ledger["counts"]["independently_verified"], 1)
        self.assertEqual(len(ledger["cases"]), 1)
        self.assertEqual(ledger["cases"][0]["exec_ids"], ["EXEC-1", "EXEC-2"])
        self.assertEqual(ledger["cases"][0]["information"], ["private-note"])
        self.assertIsNone(ledger["rate"])
        self.assertNotIn("CANARY-RAW-SECRET", json.dumps(ledger))
        self.assertNotIn("token=private", json.dumps(ledger))

    def test_model_kind_and_identity_cannot_multiply_one_surface(self):
        first = step(1, "attempted")
        second = step(2)
        second["verification_kind"] = "disclosure"
        second["verification"]["kind"] = "disclosure"
        second["identity"] = "owner"
        ledger = reduce_j25_evidence([first, second], {"inputs": []})
        self.assertEqual(len(ledger["cases"]), 1)
        self.assertEqual(ledger["counts"]["independently_verified"], 1)
        self.assertEqual(ledger["cases"][0]["kind"], "disclosure")
        self.assertEqual(ledger["cases"][0]["identity"], "owner")
        self.assertEqual(ledger["cases"][0]["exec_ids"], ["EXEC-1", "EXEC-2"])
        both_verified = reduce_j25_evidence([step(3), second], {"inputs": []})
        self.assertEqual(both_verified["counts"]["independently_verified"], 1)
        self.assertEqual(len(both_verified["cases"][0]["proofs"]), 2)

    def test_distinct_surfaces_and_incomplete_proof_stay_separate(self):
        rows = [step(1), step(2, "partial", url="https://fixture.invalid/other",
                reason="unstable_baseline"), step(3, "failed", url="https://fixture.invalid/failure",
                reason="mutation_outside_test_scope")]
        ledger = reduce_j25_evidence(rows, {"inputs": []})
        self.assertEqual(len(ledger["cases"]), 3)
        self.assertEqual(ledger["counts"]["independently_verified"], 1)
        self.assertEqual(ledger["counts"]["partial"], 1)
        self.assertEqual(ledger["counts"]["failed"], 1)
        self.assertEqual(next(item for item in ledger["cases"] if item["status"] == "partial")
                         ["missing_checks"], ["unstable_baseline"])

    def test_attempted_reflection_cannot_gain_verified_credit(self):
        row = step(1, "attempted", reason="no_information_proof")
        row["comparison"]["samples"][1]["body_sha256"] = "different"
        ledger = reduce_j25_evidence([row], {"inputs": []})
        self.assertEqual(ledger["counts"]["independently_verified"], 0)
        self.assertEqual(ledger["cases"][0]["status"], "attempted")

    def test_persisted_ledger_is_read_without_recomputing_old_runs(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            log = SessionLog(root, RUN_ID, {"prompt_version": "J2.5"})
            ledger = reduce_j25_evidence([step(1)], {"inputs": []})
            log.update(j25_state={"evidence": ledger, "steps": [step(1)]}, run_result="completed")
            log.close()
            saved = SessionStore(root).read(RUN_ID)
            self.assertEqual(saved["j25_state"]["evidence"], ledger)
            self.assertEqual(score_run(saved, root / RUN_ID)["j25_metrics"]["verified_findings"], 1)
            old = {"prompt_version": "J2.5", "j25_state": {"steps": [step(1)]}, "run_result": "completed"}
            self.assertIsNone(score_run(old, root / RUN_ID)["j25_metrics"]["verified_findings"])


if __name__ == "__main__":
    unittest.main()

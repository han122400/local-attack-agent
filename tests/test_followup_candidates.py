"""Evidence-backed follow-up discovery, without network or automatic verdicts."""

import copy
import unittest

from engine.followup import followup_candidates, unverified_lead_candidates


def surface():
    return {"kind": "surface", "locator": {
        "url": "https://example.test/session", "method": "POST",
        "input": "email,password", "auth_context": "anonymous"}, "hypothesis": None}


def observation(**changes):
    facts = {"security_signal": "auth_bypass", "input_control": "full",
             "object_scope": "self", "data_exposure": "sensitive",
             "query_behavior": "controlled_difference", "baseline_compared": True,
             "baseline_status": 401, "response_status": 200, "reproduced": False}
    facts.update(changes)
    return {"observations": facts, "proposed_verdict": "inconclusive", "discovered": [],
            "evidence_refs": [{"path": "work/login-comparison.txt", "description": "Control/test response"}]}


class FollowupCandidateTests(unittest.TestCase):
    def test_unverified_signal_creates_unknown_observation_lead_without_strong_conditions(self):
        result = observation(security_signal="account_enumeration", object_scope="unknown")
        result["evidence_refs"] = []
        verification = {"policy": "supported_observations_only", "fields": {
            "security_signal": {"state": "unsupported", "value": "account_enumeration"}}}
        leads = unverified_lead_candidates(surface(), result, verification)
        self.assertEqual(len(leads), 1)
        self.assertEqual(leads[0]["signal"], "account_enumeration")
        candidate = leads[0]["discovery"]
        self.assertEqual(candidate["hypothesis"]["type"], "auth")
        self.assertNotIn("observations", candidate)
        self.assertNotIn("evidence_refs", candidate)
        self.assertEqual(result["observations"]["security_signal"], "account_enumeration")

    def test_unverified_difference_alone_is_a_lead_but_supported_and_duplicate_are_not(self):
        result = observation(security_signal="unknown", query_behavior="controlled_difference")
        verification = {"policy": "supported_observations_only", "fields": {
            "query_behavior": {"state": "unsupported", "value": "controlled_difference"}}}
        leads = unverified_lead_candidates(surface(), result, verification)
        self.assertEqual(len(leads), 1)
        self.assertEqual(leads[0]["signal"], "controlled_difference")
        self.assertEqual(leads[0]["discovery"]["hypothesis"]["type"], "input_behavior")
        result["discovered"] = [leads[0]["discovery"]]
        self.assertEqual(unverified_lead_candidates(surface(), result, verification), [])
        result["discovered"] = []
        verification["fields"]["query_behavior"]["state"] = "supported"
        self.assertEqual(unverified_lead_candidates(surface(), result, verification), [])
        self.assertEqual(unverified_lead_candidates({**surface(), "kind": "candidate"}, result, verification), [])

    def test_existing_supported_followup_wins_the_same_dedup_key(self):
        result = observation(security_signal="account_enumeration", object_scope="cross_user")
        result["discovered"] = followup_candidates(surface(), result)
        self.assertEqual(len(result["discovered"]), 1)
        verification = {"policy": "supported_observations_only", "fields": {
            "security_signal": {"state": "unsupported", "value": "account_enumeration"}}}
        self.assertEqual(unverified_lead_candidates(surface(), result, verification), [])

    def test_auth_signal_creates_unverified_candidate_with_its_own_evidence(self):
        result = observation()
        candidates = followup_candidates(surface(), result)
        self.assertEqual(len(candidates), 1)
        candidate = candidates[0]
        self.assertEqual(candidate["kind"], "candidate")
        self.assertEqual(candidate["relation"], "refines")
        self.assertEqual(candidate["hypothesis"]["type"], "auth")
        self.assertEqual(candidate["locator"]["url"], "https://example.test/session")
        self.assertEqual(candidate["observations"]["baseline_status"], 401)
        self.assertEqual(candidate["observations"]["response_status"], 200)
        self.assertFalse(candidate["observations"]["reproduced"])
        self.assertEqual(candidate["evidence_refs"][0]["path"], "work/login-comparison.txt")
        self.assertNotIn("proposed_verdict", candidate)
        self.assertNotIn("verification_state", candidate)
        candidate["observations"]["response_status"] = 201
        candidate["evidence_refs"].clear()
        self.assertEqual(result["observations"]["response_status"], 200)
        self.assertEqual(len(result["evidence_refs"]), 1)

    def test_supported_signals_map_to_specific_investigations(self):
        cases = [
            ({"security_signal": "weak_credentials"}, "auth"),
            ({"security_signal": "account_enumeration", "object_scope": "cross_user",
              "data_exposure": "public"}, "auth"),
            ({"security_signal": "query_injection", "query_behavior": "error",
              "data_exposure": "unknown", "response_status": 500}, "sqli"),
            ({"security_signal": "unescaped_reflection", "reflection": "unescaped",
              "data_exposure": "public"}, "xss"),
            ({"security_signal": "cross_user_access", "object_scope": "cross_user"}, "idor"),
        ]
        for changes, expected in cases:
            with self.subTest(signal=changes["security_signal"]):
                candidates = followup_candidates(surface(), observation(**changes))
                self.assertEqual(len(candidates), 1)
                self.assertEqual(candidates[0]["hypothesis"]["type"], expected)

    def test_normal_login_generic_error_and_confirmed_claim_are_not_promoted(self):
        for changes in ({"security_signal": "unknown"},
                        {"security_signal": "unknown", "response_status": 500, "query_behavior": "error"},
                        {"security_signal": "auth_bypass", "response_status": 401},
                        {"security_signal": "account_enumeration", "object_scope": "self"},
                        {"security_signal": "cross_user_access", "object_scope": "public"},
                        {"security_signal": "unescaped_reflection", "reflection": "escaped"}):
            with self.subTest(changes=changes):
                result = observation(**changes)
                result["proposed_verdict"] = "confirmed"
                self.assertEqual(followup_candidates(surface(), result), [])
        result = observation()
        del result["observations"]["security_signal"]
        self.assertEqual(followup_candidates(surface(), result), [])

    def test_missing_evidence_baseline_or_control_does_not_create_a_candidate(self):
        for result in (observation(baseline_compared=False), observation(input_control="unknown"),
                       {**observation(), "evidence_refs": []}, {**observation(), "observations": {}}):
            with self.subTest(result=result):
                self.assertEqual(followup_candidates(surface(), result), [])

    def test_only_surfaces_generate_followups_and_explicit_candidate_wins(self):
        for kind in ("root", "candidate"):
            self.assertEqual(followup_candidates({**surface(), "kind": kind}, observation()), [])
        candidate = {"kind": "candidate", "relation": "refines", "locator": surface()["locator"],
                     "hypothesis": {"type": "auth", "statement": "Specific model-authored hypothesis"}}
        result = observation()
        result["discovered"] = [candidate]
        original = copy.deepcopy(result)
        self.assertEqual(followup_candidates(surface(), result), [])
        self.assertEqual(result, original)


if __name__ == "__main__":
    unittest.main()

import unittest

from j25_verifiers import verify_candidate


def samples(test_hits=(), *, stable=True):
    return {"samples": [
        {"stage": "normal", "status": 200, "body_sha256": "a", "canary_hits": []},
        {"stage": "test", "status": 200, "body_sha256": "b", "canary_hits": list(test_hits)},
        {"stage": "recheck", "status": 200, "body_sha256": "a" if stable else "c", "canary_hits": []},
    ], "test_value": "probe", "identity": "public", "method": "GET"}


class CandidateVerifierTests(unittest.TestCase):
    def setUp(self):
        self.scope = {"canaries": {"private-note": {"protected": True,
                       "authorized_identities": ["owner"]}}, "mutation_allowed": False}

    def test_private_information_recovered_by_a_test_is_verified_across_supported_categories(self):
        for kind in ("injection", "disclosure"):
            with self.subTest(kind=kind):
                verdict = verify_candidate(kind, samples(["private-note"]), scope=self.scope)
                self.assertEqual(verdict["status"], "independently_verified")
                self.assertEqual(verdict["information"], ["private-note"])

    def test_authorization_claim_needs_a_real_second_identity(self):
        self.assertNotEqual(verify_candidate("authorization", samples(["private-note"]),
                                            scope=self.scope)["status"], "independently_verified")

    def test_browser_input_needs_execution_proof_as_well_as_private_information(self):
        data = samples(["private-note"])
        self.assertNotEqual(verify_candidate("browser_input", data, scope=self.scope)["status"],
                            "independently_verified")
        data["execution_proof"] = {"executed": True, "canary_hits": ["private-note"]}
        self.assertEqual(verify_candidate("browser_input", data, scope=self.scope)["status"],
                         "independently_verified")

    def test_browser_execution_can_read_private_dom_data_in_owners_session(self):
        data = samples(["private-note"])
        data["identity"] = "owner"
        data["samples"][0]["canary_hits"] = ["private-note"]
        data["samples"][2]["canary_hits"] = ["private-note"]
        data["execution_proof"] = {"executed": True, "recheck_executed": False,
                                   "canary_hits": ["private-note"]}
        self.assertEqual(verify_candidate("browser_input", data, scope=self.scope)["status"],
                         "independently_verified")

    def test_hash_change_reflection_and_public_data_do_not_count_as_vulnerabilities(self):
        self.assertEqual(verify_candidate("injection", samples(), scope=self.scope)["status"], "attempted")
        public = {"canaries": {"private-note": {"protected": False,
                   "authorized_identities": ["public"]}}, "mutation_allowed": False}
        self.assertNotEqual(verify_candidate("disclosure", samples(["private-note"]), scope=public)["status"],
                            "independently_verified")

    def test_unstable_baseline_and_unapproved_mutation_keep_partial_or_failed_evidence(self):
        self.assertEqual(verify_candidate("injection", samples(["private-note"], stable=False),
                                          scope=self.scope)["status"], "partial")
        post = samples(["private-note"])
        post["method"] = "POST"
        self.assertEqual(verify_candidate("injection", post, scope=self.scope)["status"], "failed")

    def test_missing_private_oracle_cannot_be_independently_verified(self):
        result = verify_candidate("authorization", samples(["private-note"]), scope={"canaries": {}})
        self.assertNotEqual(result["status"], "independently_verified")

    def test_cross_identity_access_requires_owner_and_unauthorized_reader_to_obtain_same_private_marker(self):
        observation = {"identity": "public", "owner_identity": "owner", "access_samples": [
            {"stage": "unauthorized", "status": 200, "body_sha256": "a", "canary_hits": ["private-note"]},
            {"stage": "owner", "status": 200, "body_sha256": "b", "canary_hits": ["private-note"]},
            {"stage": "recheck", "status": 200, "body_sha256": "a", "canary_hits": ["private-note"]},
        ]}
        checked = verify_candidate("authorization", observation, scope=self.scope)
        self.assertEqual(checked["status"], "independently_verified")
        observation["access_samples"][1]["canary_hits"] = []
        self.assertNotEqual(verify_candidate("authorization", observation, scope=self.scope)["status"],
                            "independently_verified")

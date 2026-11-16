import unittest
import threading
from urllib.error import HTTPError
from urllib.request import urlopen

from benchmarks.j25_blackbox import evaluate_runs, validate_manifest
from benchmarks.j25_fixture import MANIFEST, make_server, oracle, test_scope
from explorer import validate_j25_test_scope


def manifest():
    kinds = ["authorization", "injection", "browser_input", "disclosure"]
    cases = [{"id": f"case-{n}", "category": kinds[n % 4], "split": "holdout" if n < 10 else "development",
              "surface": f"/case/{n}", "field": "q", "kind": kinds[n % 4],
              "information_label": f"canary-{n}", "oracle_id": n, "eligible": True}
             for n in range(20)]
    return {"version": "fixture-v1", "cases": cases,
            "controls": [{"id": "safe-1", "surface": "/safe/1"}]}


def run(reset_id, *, attempted=(), solved=(), control_claims=()):
    cases = [{"id": f"ledger-{n}", "surface": f"/case/{n}", "field": "q",
              "kind": ["authorization", "injection", "browser_input", "disclosure"][n % 4],
              "status": "independently_verified", "information": [f"canary-{n}"],
              "proofs": [{"body_sha256": "a" * 64}]} for n in attempted]
    return {"split": "holdout", "reset_id": reset_id, "model": "local", "elapsed_s": 12,
            "model_calls": 5, "request_count": 10, "cost_usd": 0,
            "ledger": {"cases": cases}, "oracle": {"status": "available", "solved_ids": list(solved)},
            "control_claims": list(control_claims)}


class BlackboxTests(unittest.TestCase):
    def test_final_holdout_is_new_and_uses_independent_routes(self):
        final = [case for case in MANIFEST["cases"] if case["split"] == "holdout"]
        self.assertEqual(len(final), 10)
        self.assertTrue(all(case["oracle_id"] >= 20 for case in final))
        self.assertEqual(len({case["surface"] for case in final}), 10)
        self.assertTrue(all(case["surface"].startswith("/records/") for case in final))

    def test_fixture_serves_cases_but_never_serves_manifest_or_oracle(self):
        server = make_server("holdout")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            target = f"http://127.0.0.1:{server.server_port}/"
            validate_j25_test_scope(test_scope(server, "holdout"), target, "J2.5")
            with urlopen(target) as response:
                index = response.read().decode()
            self.assertIn("/records/amber", index)
            self.assertNotIn("J25-FIXTURE-PRIVATE", index)
            with urlopen(target + "safe/0") as response:
                self.assertNotIn("J25-FIXTURE-PRIVATE", response.read().decode())
            with self.assertRaises(HTTPError) as caught:
                urlopen(target + "oracle")
            caught.exception.close()
            self.assertEqual(len(oracle("holdout")["truth_ids"]), 10)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(5)

    def test_preregistration_requires_twenty_four_and_balanced_holdout(self):
        source = manifest()
        validate_manifest(source)
        with self.assertRaises(ValueError):
            validate_manifest({**source, "cases": source["cases"][:19]})
        unbalanced = manifest()
        for item in unbalanced["cases"][:10]:
            item["category"] = "injection"
        with self.assertRaises(ValueError):
            validate_manifest(unbalanced)
        duplicate = manifest()
        duplicate["cases"][1]["oracle_id"] = duplicate["cases"][0]["oracle_id"]
        with self.assertRaises(ValueError):
            validate_manifest(duplicate)
        duplicate = manifest()
        duplicate["cases"][1]["surface"] = duplicate["cases"][0]["surface"]
        with self.assertRaises(ValueError):
            validate_manifest(duplicate)

    def test_active_unsupported_or_unattempted_cases_stay_in_denominator(self):
        source = manifest()
        source["cases"][1]["support"] = "unsupported"
        source["cases"].append({"id": "disabled", "category": "injection", "split": "holdout",
            "surface": "/disabled", "field": "q", "kind": "injection", "information_label": "none",
            "oracle_id": 99, "eligible": False, "exclusion": "docker_disabled"})
        rows = [run(f"reset-{n}", attempted=(0,), solved=(0,), control_claims=("safe-1",)) for n in range(3)]
        result = evaluate_runs(source, rows)
        self.assertEqual(result["holdout_denominator"], 10)
        self.assertEqual(result["excluded_count"], 1)
        self.assertEqual(result["unsupported_active_count"], 1)
        self.assertEqual(result["runs"][0]["verified_count"], 1)
        self.assertEqual(result["runs"][0]["unattempted_count"], 9)
        self.assertEqual(result["runs"][0]["false_positive_controls"], 1)
        self.assertEqual(result["runs"][0]["control_attempted_count"], 0)
        self.assertEqual(result["runs"][0]["rate"], 0.1)

    def test_verified_information_counts_even_if_model_mislabels_category(self):
        source = manifest()
        rows = [run(f"reset-{n}", attempted=(0,), solved=(0,)) for n in range(3)]
        for row in rows:
            row["ledger"]["cases"][0]["kind"] = "disclosure"
        result = evaluate_runs(source, rows)
        self.assertEqual(result["runs"][0]["verified_case_ids"], ["case-0"])
        self.assertEqual(result["runs"][0]["category_coverage"], {"authorization": 1})

    def test_three_distinct_resets_and_independent_oracle_are_required(self):
        source = manifest()
        with self.assertRaises(ValueError):
            evaluate_runs(source, [run("same"), run("same"), run("other")])
        result = evaluate_runs(source, [run(str(n), attempted=(0,), solved=()) for n in range(3)])
        self.assertEqual([row["verified_count"] for row in result["runs"]], [0, 0, 0])


if __name__ == "__main__":
    unittest.main()

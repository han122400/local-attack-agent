"""Known offline text fixtures, not vulnerability detection performance."""

import unittest

from benchmarks.j2_pipeline_replay import evaluate, fixture_decision


class ReplayTests(unittest.TestCase):
    def test_same_fixture_planner_uses_progress_to_avoid_reanalyzing_first_resource(self):
        baseline = evaluate(fixture_decision, progress=False, max_calls=6)
        improved = evaluate(fixture_decision, progress=True, max_calls=6)
        self.assertEqual(baseline["analyzed_expected_resources"], 1)
        self.assertEqual(baseline["missed_expected_resources"], 1)
        self.assertEqual(baseline["redundant_analyses"], 5)
        self.assertFalse(baseline["finished"])
        self.assertEqual(improved["analyzed_expected_resources"], 2)
        self.assertEqual(improved["missed_expected_resources"], 0)
        self.assertEqual(improved["redundant_analyses"], 0)
        self.assertEqual(improved["decision_calls"], 3)
        self.assertTrue(improved["finished"])

    def test_premature_completion_is_measured_against_known_fixture_facts(self):
        result = evaluate(lambda _: {"action": "finish", "claims_complete": True}, progress=True)
        self.assertEqual(result["missed_expected_resources"], 2)
        self.assertEqual(result["unsupported_completion_claims"], 1)
        self.assertEqual(result["external_requests"], 0)

    def test_unknown_reference_is_not_fetched_or_counted_as_completed_work(self):
        result = evaluate(lambda _: {"action": "analyze", "url": "https://outside.invalid/"}, max_calls=1)
        self.assertEqual(result["invalid_actions"], 1)
        self.assertEqual(result["analyzed_expected_resources"], 0)
        self.assertEqual(result["external_requests"], 0)

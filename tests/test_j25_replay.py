import unittest

from benchmarks.j25_replay import evaluate


class ReplayTests(unittest.TestCase):
    def test_scripted_comparison_records_both_decision_formats_without_real_requests(self):
        report = evaluate()
        self.assertIn("scripted", report["scope"])
        self.assertEqual(report["external_requests"], 0)
        self.assertEqual([row["first_comparison_step"] for row in report["results"]], [1, 2])
        self.assertEqual([row["model_calls"] for row in report["results"]], [2, 2])
        self.assertEqual([row["generated_code_commands"] for row in report["results"]], [1, 0])
        self.assertEqual(report["results"][1].get("comparison_status"), "ok")

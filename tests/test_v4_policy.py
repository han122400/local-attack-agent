import unittest

from v4_policy import execution_settings


class PolicyTests(unittest.TestCase):
    def test_settings_are_versioned_and_reject_unknown_or_bad_values(self):
        settings = execution_settings({})
        self.assertEqual(settings["version"], "v4-execution-v2")
        for value in ({"node_attempts": 0}, {"max_cost_usd": float("nan")}, {"api_key": "secret"}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                execution_settings(value)

    def test_removed_lab_verifier_options_cannot_be_enabled_for_new_runs(self):
        for key, value in (("exclusive_lab", True), ("oracle_max_calls", 10),
                           ("oracle_timeout_seconds", 5), ("verification_reserve_seconds", 25)):
            with self.subTest(key=key), self.assertRaises(ValueError):
                execution_settings({key: value})


if __name__ == "__main__":
    unittest.main()

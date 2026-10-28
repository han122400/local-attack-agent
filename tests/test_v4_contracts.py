"""Untrusted result and experiment configuration boundary tests."""

import copy
import unittest

from v4_contracts import ContractError, dedup_key, validate_discovery, validate_result
from experiment_config import validate_experiment_config


def discovery(**changes):
    value = {"kind": "candidate", "relation": "discovered",
             "locator": {"url": "https://example.test/search", "method": "GET",
                         "input": "q", "auth_context": "anonymous"},
             "hypothesis": {"type": "sqli", "statement": "Search input may affect a query"}}
    value.update(changes)
    return value


def result(**changes):
    value = {"schema_version": 1, "attempt_id": "A0001", "node_id": "N0001",
             "discovered": [discovery()], "proposed_verdict": "confirmed",
             "claim": "Model says it worked", "evidence_refs": [
                 {"path": "work/evidence/response.txt", "description": "Captured response"}],
             "next_observation": "Compare with the baseline"}
    value.update(changes)
    return value


class ResultContractTests(unittest.TestCase):
    def test_normalizes_locations_without_promoting_the_claim(self):
        value = result(discovered=[discovery(locator={"url": "https://EXAMPLE.test:443/search#field",
                    "method": "get", "input": "q", "auth_context": "anonymous"})])
        normalized = validate_result(value, attempt_id="A0001", node_id="N0001")
        self.assertEqual(normalized["discovered"][0]["locator"]["url"], "https://example.test/search")
        self.assertEqual(normalized["discovered"][0]["locator"]["method"], "GET")
        self.assertEqual(normalized["proposed_verdict"], "confirmed")
        self.assertNotIn("verification_state", normalized)
        self.assertEqual(value["discovered"][0]["locator"]["method"], "get")

    def test_rejects_wrong_attempt_node_version_and_harness_owned_fields(self):
        for change in ({"attempt_id": "A0002"}, {"node_id": "N0002"}, {"schema_version": 2},
                       {"schema_version": True}, {"verification_state": "confirmed"},
                       {"discovered": [discovery(id="N9999")]}, {"discovered": "not a list"}):
            with self.subTest(change=change), self.assertRaises(ContractError):
                validate_result(result(**change), attempt_id="A0001", node_id="N0001")

    def test_rejects_escaping_or_non_exported_evidence(self):
        for path in ("../outside", "/secret", "C:/secret", "work/../secret", "work\\file",
                     "runtime/file.txt", "artifacts/old.txt", "work/file:stream", "work/NUL",
                     "work/node-results/A0001.json"):
            with self.subTest(path=path), self.assertRaises(ContractError):
                validate_result(result(evidence_refs=[{"path": path, "description": "x"}]),
                                attempt_id="A0001", node_id="N0001")

    def test_candidate_requires_hypothesis_and_cannot_supply_a_root(self):
        for value in (discovery(kind="root"), discovery(hypothesis=None),
                      discovery(relation="invented"), discovery(locator={"url": "file:///secret"})):
            with self.subTest(value=value), self.assertRaises(ContractError):
                validate_discovery(value)

    def test_dedup_preserves_method_input_auth_and_hypothesis_boundaries(self):
        baseline = validate_discovery(discovery())
        duplicate = discovery(hypothesis={"type": "SQLI", "statement": "Different wording"})
        self.assertEqual(dedup_key(baseline), dedup_key(validate_discovery(duplicate)))
        for key, replacement in (("method", "POST"), ("input", "id"), ("auth_context", "owner"),
                                 ("url", "https://other.test/search")):
            changed = copy.deepcopy(baseline)
            changed["locator"][key] = replacement
            self.assertNotEqual(dedup_key(baseline), dedup_key(changed))
        changed = discovery(hypothesis={"type": "xss", "statement": "Reflection"})
        self.assertNotEqual(dedup_key(baseline), dedup_key(validate_discovery(changed)))


class ExperimentConfigTests(unittest.TestCase):
    def test_v3_defaults_and_ignores_inactive_scorer_settings(self):
        config = validate_experiment_config({"scorer_provider": "bad", "scorer_settings": {"key": "secret"}})
        self.assertEqual(config, {"experiment_mode": "v3", "scorer_provider": None,
                                  "scorer_model": None, "scorer_settings": {}})

    def test_v4_pins_separate_settings_without_inventing_defaults(self):
        for provider, model in (("openrouter", "typesafe/jev-1.14"), ("local", "local-judge:1")):
            config = validate_experiment_config({"experiment_mode": "v4", "scorer_provider": provider,
                                                  "scorer_model": model, "scorer_settings": {"max_retries": 0}})
            self.assertEqual(config["scorer_provider"], provider)
            self.assertEqual(config["scorer_settings"], {"max_retries": 0})

    def test_rejects_unknown_modes_missing_models_and_secret_or_generation_settings(self):
        baseline = {"experiment_mode": "v4", "scorer_provider": "openrouter",
                    "scorer_model": "typesafe/jev-1.14", "scorer_settings": {}}
        for change in ({"experiment_mode": "v5"}, {"scorer_provider": "other"}, {"scorer_model": ""},
                       {"scorer_model": "~typesafe/jev-latest"}, {"scorer_settings": {"api_key": "secret"}},
                       {"scorer_settings": {"temperature": 0.2}}, {"scorer_settings": {"batch_size": True}},
                       {"scorer_settings": {"timeout_seconds": float("nan")}},
                       {"scorer_settings": {"max_calls": 2, "max_calls_per_parent": 3}}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_experiment_config({**baseline, **change})


if __name__ == "__main__":
    unittest.main()

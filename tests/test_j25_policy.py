import json
import tempfile
import unittest
from pathlib import Path

from harness import Harness
from j25_policy import J25_SCHEMA, j25_schema_for_context, pack_j25_context, validate_j25_decision
from model_providers import _request_payload
from session_log import SessionLog


RUN_ID = "9" * 32


class J25DecisionTests(unittest.TestCase):
    def test_focused_verification_schema_hides_unrelated_navigation(self):
        context = {"references": {"pages": [{"id": "P1"}], "assets": [],
            "inputs": [{"id": "I1", "supported": True}]},
            "focus": {"action": "verify", "refs": ["I1"]}}
        schema = j25_schema_for_context(context, report_only=False)
        self.assertEqual([branch["properties"]["action"]["const"] for branch in schema["oneOf"]],
                         ["verify", "finish"])
        self.assertEqual(schema["oneOf"][0]["properties"]["ref"]["enum"], ["I1"])

    def test_schema_only_offers_visible_supported_references(self):
        context = {"references": {"pages": [{"id": "P1", "url": "https://fixture.invalid/?token=private"}],
                                  "assets": [{"id": "A1"}],
                                  "inputs": [{"id": "I1", "supported": False}]}}
        schema = j25_schema_for_context(context, report_only=False)
        branches = {item["properties"]["action"]["const"]: item for item in schema["oneOf"]}
        self.assertEqual(branches["observe"]["properties"]["ref"]["enum"], ["P1"])
        self.assertEqual(branches["analyze"]["properties"]["ref"]["enum"], ["A1"])
        self.assertNotIn("compare", branches)
        self.assertNotIn("private", json.dumps(schema))
        self.assertEqual([item["properties"]["action"]["const"] for item in
                          j25_schema_for_context({"references": {}}, report_only=False)["oneOf"]], ["finish"])

    def test_compacted_reference_cannot_be_used_even_if_it_was_in_the_original_context(self):
        original = {"goal": "inspect", "reference_order": "priority", "references": {
            "pages": [{"id": "P1", "label": "first"}, {"id": "P2", "label": "x" * 900}],
            "assets": [], "inputs": []}}
        packed = pack_j25_context(original, 420)
        schema = j25_schema_for_context(packed, report_only=False)
        self.assertEqual(schema["oneOf"][0]["properties"]["ref"]["enum"], ["P1"])
        with self.assertRaisesRegex(ValueError, "visible context"):
            validate_j25_decision({"action": "observe", "ref": "P2", "reason": "inspect"},
                                  references=packed["references"])

    def test_interact_and_replay_require_matching_observed_capabilities(self):
        refs = {"inputs": [{"id": "visible", "interactable": True, "supported": False},
                           {"id": "request", "replayable": True, "supported": True},
                           {"id": "post", "replayable": False, "supported": False}]}
        schema = j25_schema_for_context({"references": refs}, report_only=False)
        branches = {item["properties"]["action"]["const"]: item for item in schema["oneOf"]}
        self.assertEqual(branches["interact"]["properties"]["ref"]["enum"], ["visible"])
        self.assertEqual(branches["replay"]["properties"]["ref"]["enum"], ["request"])
        with self.assertRaisesRegex(ValueError, "visible context"):
            validate_j25_decision({"action": "replay", "ref": "post", "value": "x", "reason": "try"},
                                  references=refs)

    def test_identity_action_only_names_allowed_test_sessions(self):
        context = {"references": {}, "identities": ["public", "test-user"], "current_identity": "public"}
        schema = j25_schema_for_context(context, report_only=False)
        branch = next(item for item in schema["oneOf"] if item["properties"]["action"]["const"] == "identity")
        self.assertEqual(branch["properties"]["value"]["enum"], ["test-user"])
        self.assertEqual(validate_j25_decision({"action": "identity", "value": "test-user", "reason": "compare"},
            identities=context["identities"], current_identity="public").action, "identity")
        with self.assertRaisesRegex(ValueError, "test identity"):
            validate_j25_decision({"action": "identity", "value": "outsider", "reason": "switch"},
                                  identities=context["identities"], current_identity="public")

    def test_verify_action_chooses_a_supported_module_and_visible_input(self):
        refs = {"inputs": [{"id": "I1", "supported": True}, {"id": "I2", "supported": False}]}
        schema = j25_schema_for_context({"references": refs}, report_only=False)
        branch = next(item for item in schema["oneOf"] if item["properties"]["action"]["const"] == "verify")
        self.assertEqual(branch["properties"]["ref"]["enum"], ["I1"])
        self.assertIn("injection", branch["properties"]["kind"]["enum"])
        self.assertEqual(branch["properties"]["kind"]["enum"], ["injection", "disclosure"])
        decision = {"action": "verify", "ref": "I1", "kind": "injection", "reason": "test",
                    "hypothesis": "private data", "normal": "plain", "test": "probe"}
        self.assertEqual(validate_j25_decision(decision, references=refs).kind, "injection")
        with self.assertRaises(ValueError):
            validate_j25_decision({**decision, "kind": "arbitrary"}, references=refs)

    def test_access_verification_uses_visible_page_and_another_test_identity(self):
        context = {"references": {"pages": [{"id": "P1", "supported": True}]},
                   "identities": ["public", "owner"], "current_identity": "public"}
        schema = j25_schema_for_context(context, report_only=False)
        branch = next(item for item in schema["oneOf"] if item["properties"]["action"]["const"] == "verify_access")
        self.assertEqual(branch["properties"]["ref"]["enum"], ["P1"])
        self.assertEqual(branch["properties"]["value"]["enum"], ["owner"])
        self.assertEqual(validate_j25_decision({"action": "verify_access", "ref": "P1",
            "value": "owner", "reason": "compare access"}, references=context["references"],
            identities=context["identities"], current_identity="public").action, "verify_access")

    def test_only_fixed_actions_and_valid_comparison_fields_are_accepted(self):
        result = validate_j25_decision({"action": "compare", "ref": "I1", "reason": "test input",
                                        "hypothesis": "different processing", "normal": "hello", "test": "hello2"})
        self.assertEqual((result.action, result.ref), ("compare", "I1"))
        for payload in (
            {"action": "run_python", "code": "print(1)", "reason": "x"},
            {"action": "observe", "ref": "P1", "reason": "x", "code": "print(1)"},
            {"action": "compare", "ref": "I1", "reason": "x", "hypothesis": "x", "normal": "same", "test": "same"},
            {"action": "finish", "reason": "done", "goal_status": "verified"},
        ):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                validate_j25_decision(payload)

    def test_compaction_keeps_latest_of_each_kind_when_possible(self):
        context = {"goal": "inspect", "remaining": {}, "references": {
            kind: [{"id": f"{kind}-{i}", "label": "x" * 180} for i in range(5)]
            for kind in ("pages", "assets", "inputs")}}
        packed = pack_j25_context(context, 1300)
        self.assertLessEqual(len(json.dumps(packed, ensure_ascii=False)), 1300)
        for kind in ("pages", "assets", "inputs"):
            self.assertEqual(packed["references"][kind][-1]["id"], f"{kind}-4")
        self.assertTrue(packed["truncated"])

    def test_old_recent_results_are_removed_before_the_last_reference(self):
        refs = {kind: [{"id": kind, "label": "x" * 100}] for kind in ("pages", "assets", "inputs")}
        context = {"goal": "inspect", "references": refs, "recent": [{"data": "x" * 3000}]}
        packed = pack_j25_context(context, 1200)
        self.assertTrue(all(packed["references"][kind] for kind in refs))
        self.assertEqual(packed["recent"], [])

    def test_compaction_keeps_high_priority_reference_before_noisy_candidates(self):
        context = {"goal": "inspect search", "reference_order": "priority",
            "references": {"pages": [], "assets": [], "inputs": [
                {"id": "search", "field": "q", "label": "x" * 100},
                *[{"id": f"socket-{i}", "label": "y" * 100} for i in range(20)]]},
            "pending": ["search", *[f"socket-{i}" for i in range(20)]]}
        packed = pack_j25_context(context, 1000)
        self.assertLessEqual(len(json.dumps(packed, ensure_ascii=False)), 1000)
        self.assertEqual(packed["references"]["inputs"][0]["id"], "search")
        self.assertGreater(packed["context_omitted"]["inputs"], 0)
        self.assertTrue(set(packed["pending"]) <= {item["id"] for item in packed["references"]["inputs"]})

    def test_portable_schema_accepts_empty_unused_fields(self):
        info = {"id": "fixture", "supported_parameters": ["structured_outputs"],
                "max_completion_tokens": 1000, "context_length": 8192}
        payload = {"format": J25_SCHEMA, "messages": [], "options": {}}
        request = _request_payload(payload, info)
        self.assertEqual(request["response_format"]["type"], "json_schema")
        decision = validate_j25_decision({"action": "observe", "ref": "P1", "reason": "inspect",
                                          "normal": "", "test": "", "hypothesis": ""})
        self.assertEqual(decision.action, "observe")

    def test_harness_retries_bad_decisions_and_reserves_reporting(self):
        with tempfile.TemporaryDirectory() as temp:
            log = SessionLog(Path(temp), RUN_ID, {})
            self.addCleanup(log.close)
            sent = []
            def transport(payload):
                sent.append(payload)
                value = ({"action": "run_python", "reason": "x"} if len(sent) == 1 else
                         {"action": "observe", "ref": "P1", "reason": "inspect"} if len(sent) == 2 else
                         {"action": "finish", "reason": "report"})
                yield {"message": {"content": json.dumps(value)}, "done": True}
            harness = Harness(log, transport=transport, max_calls=3, report_reserve=1,
                              system_prompt="Short decisions", system_prompt_version="J2.5")
            decision = harness.decide(RUN_ID, {"goal": "inspect", "references": {"pages": [{"id": "P1"}]},
                                               "remaining": {"finish_soon": False}},
                                      model="fixture", settings={})
            self.assertEqual(decision.action, "observe")
            report = harness.decide(RUN_ID, {"goal": "inspect", "remaining": {"finish_soon": True}},
                                    model="fixture", settings={})
            self.assertEqual(report.action, "finish")
            self.assertEqual(harness.calls, 3)

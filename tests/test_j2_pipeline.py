"""Pipeline bookkeeping with synthetic declarations; no browser, target or model."""

import json
import unittest
import tempfile
from pathlib import Path

from command_runtime import ExecutionResult
from j2_policy import CandidateNotebook, J2Policy, pack_j2_context
from scorecard import score_run


ROOT = "https://fixture.invalid/"
SCRIPT = ROOT + "assets/exact-app-hash.js"


class ProgressTests(unittest.TestCase):
    def declared(self):
        policy = J2Policy()
        policy.record(ExecutionResult("E1", "run_python", exit_code=0, stdout="source",
            discovery_summaries=[{"source_url": ROOT, "assets": [{"url": SCRIPT, "kind": "script"}]}]), 1)
        return policy

    def test_http_success_and_plain_stdout_do_not_mean_source_analysis(self):
        policy = self.declared()
        policy.record(ExecutionResult("E2", "run_python", exit_code=0, stdout="downloaded",
            http_observations=[{"url": SCRIPT, "method": "GET", "status": 200}]), 2)
        resource = policy.snapshot()["pipeline"]["resources"][0]
        self.assertEqual(resource["declared_step"], 1)
        self.assertEqual(resource["response"]["step"], 2)
        self.assertIsNone(resource["analysis"])
        self.assertEqual(policy.context()["finish_review"]["unreviewed_resources"], 1)

    def test_analysis_of_supplied_text_is_separate_from_network_access(self):
        policy = self.declared()
        policy.record(ExecutionResult("E2", "run_python", exit_code=1, stdout="partial",
            discovery_summaries=[{"source_url": SCRIPT, "truncated": True}]), 2)
        resource = policy.snapshot()["pipeline"]["resources"][0]
        self.assertIsNone(resource["response"])
        self.assertEqual(resource["analysis"], {"step": 2, "summary_truncated": True})
        self.assertEqual(policy.context()["finish_review"]["unreviewed_resources"], 0)
        self.assertEqual(policy.context()["finish_review"]["limited_analyses"], 1)

    def test_old_response_is_linked_even_when_declaration_is_discovered_later(self):
        policy = J2Policy()
        policy.record(ExecutionResult("E1", "run_python", exit_code=0, http_observations=[
            {"url": SCRIPT, "method": "GET", "status": 404}]), 1)
        policy.record(ExecutionResult("E2", "run_python", exit_code=0, discovery_summaries=[
            {"source_url": ROOT, "assets": [{"url": SCRIPT, "kind": "script"}]}]), 2)
        resource = policy.snapshot()["pipeline"]["resources"][0]
        self.assertEqual(resource["response"]["status"], 404)
        self.assertIsNone(resource["analysis"])

    def test_truncated_metadata_does_not_match_another_resource_with_the_same_prefix(self):
        prefix = ROOT + "x" * (240 - len(ROOT))
        policy = J2Policy()
        policy.record(ExecutionResult("E1", "run_python", exit_code=0, http_observations=[
            {"url": prefix, "method": "GET", "status": 200, "url_truncated": True}], discovery_summaries=[
            {"source_url": ROOT, "assets": [{"url": prefix, "kind": "script"}]}]), 1)
        self.assertIsNone(policy.snapshot()["pipeline"]["resources"][0]["response"])

    def test_clipped_helper_paths_do_not_establish_a_recovery_link(self):
        prefix = ROOT + "x" * (240 - len(ROOT))
        policy = J2Policy()
        policy.record(ExecutionResult("E1", "run_python", exit_code=0, helper_errors=[
            {"helper": "goto_and_render", "error_type": "TimeoutError", "url": prefix + "different"}]), 1)
        policy.record(ExecutionResult("E2", "run_python", exit_code=0, helper_outcomes=[
            {"helper": "goto_and_render", "outcome": "completed", "url": prefix}]), 2)
        self.assertIsNone(policy.snapshot()["pipeline"]["failures"][0]["later_completion_step"])

    def test_bounded_resource_preview_exposes_omissions_instead_of_complete_coverage(self):
        policy = J2Policy()
        for index in range(8):
            policy.record(ExecutionResult(str(index), "run_python", exit_code=0, discovery_summaries=[{
                "source_url": ROOT + str(index), "assets": [{"url": ROOT + f"{index}-{i}.js", "kind": "script"} for i in range(20)]}]), index + 1)
        pipeline = policy.snapshot()["pipeline"]
        self.assertEqual(len(pipeline["resources"]), 128)
        self.assertEqual(pipeline["omitted_resources"], 32)
        self.assertEqual(pipeline["review"]["unreviewed_resources"], 160)
        self.assertEqual(len(policy.context()["pending_resources"]), 6)
        self.assertEqual(policy.context()["omitted_pending"], 154)

    def test_finish_review_survives_compaction_without_mandatory_traversal(self):
        policy = self.declared()
        context = {"goal": "inspect", "j2": policy.context(), "remaining": {"finish_soon": True}}
        packed = pack_j2_context(context, 2400)
        self.assertEqual(packed["j2"]["finish_review"]["unreviewed_resources"], 1)
        self.assertFalse(packed["j2"]["finish_review"]["must_visit_all"])
        self.assertEqual(packed["remaining"], context["remaining"])
        self.assertLessEqual(len(json.dumps(packed, ensure_ascii=False)), 2400)

    def test_repeated_navigation_failure_is_not_resolved_by_unrelated_output(self):
        policy = J2Policy()
        for step in (1, 2):
            policy.record(ExecutionResult(f"E{step}", "run_python", exit_code=0, stdout="caught",
                helper_errors=[{"helper": "goto_and_render", "error_type": "TimeoutError", "url": ROOT}]), step)
        policy.record(ExecutionResult("E3", "run_python", exit_code=0, stdout="unrelated"), 3)
        failure = policy.snapshot()["pipeline"]["failures"][0]
        self.assertEqual(failure["kind"], "navigation")
        self.assertEqual(failure["steps"], [1, 2])
        self.assertIsNone(failure["later_completion_step"])

    def test_later_navigation_completion_is_linked_without_erasing_failure(self):
        policy = J2Policy()
        policy.record(ExecutionResult("E1", "run_python", exit_code=0, helper_errors=[
            {"helper": "goto_and_render", "error_type": "TimeoutError", "url": ROOT}]), 1)
        policy.record(ExecutionResult("E2", "run_python", exit_code=0, helper_outcomes=[
            {"helper": "goto_and_render", "outcome": "completed", "url": ROOT + "?token=private"}]), 2)
        failure = policy.snapshot()["pipeline"]["failures"][0]
        self.assertEqual(failure["steps"], [1])
        self.assertEqual(failure["later_completion_step"], 2)
        self.assertNotIn("private", json.dumps(policy.snapshot()["executions"]))
        policy.record(ExecutionResult("E3", "run_python", exit_code=1, helper_errors=[
            {"helper": "goto_and_render", "error_type": "TimeoutError", "url": ROOT}]), 3)
        self.assertIsNone(policy.snapshot()["pipeline"]["failures"][0]["later_completion_step"])

    def test_failure_categories_do_not_turn_runtime_problems_into_target_defenses(self):
        policy = J2Policy()
        for step, values in enumerate((
                {"exception_type": "ModuleNotFoundError"}, {"timed_out": True},
                {"failure_source": "system"}, {"exception_type": "SyntaxError"}), 1):
            policy.record(ExecutionResult(str(step), "run_python", exit_code=1, **values), step)
        self.assertEqual([item["kind"] for item in policy.snapshot()["pipeline"]["failures"]],
                         ["dependency", "execution_timeout", "runtime", "code"])

    def test_caught_navigation_error_does_not_hide_a_later_terminal_code_exception(self):
        policy = J2Policy()
        policy.record(ExecutionResult("E1", "run_python", exit_code=1, exception_type="TypeError",
            helper_errors=[{"helper": "goto_and_render", "error_type": "TimeoutError", "url": ROOT}]), 1)
        failures = policy.snapshot()["pipeline"]["failures"]
        self.assertEqual([(item["kind"], item["error_type"]) for item in failures],
                         [("navigation", "TimeoutError"), ("code", "TypeError")])

    def test_recurrent_old_failure_stays_in_recent_recovery_after_many_other_errors(self):
        policy = J2Policy()
        for step in range(1, 35):
            path = "first" if step in {1, 34} else str(step)
            policy.record(ExecutionResult(str(step), "run_python", exit_code=0,
                helper_errors=[{"helper": "goto_and_render", "error_type": "TimeoutError", "url": ROOT + path}]), step)
        self.assertEqual(policy.context()["recovery"][-1]["url"], ROOT + "first")
        self.assertEqual(policy.context()["recovery"][-1]["steps"], [1, 34])

    def test_access_response_and_harness_scope_are_distinct_limits(self):
        policy = J2Policy()
        policy.record(ExecutionResult("E1", "run_python", exit_code=0, http_observations=[
            {"url": ROOT, "method": "GET", "status": 401}]), 1)
        policy.record(ExecutionResult("E2", "run_python", exit_code=0,
            blocked_requests=[{"reason": "outside_target_origin"}]), 2)
        self.assertEqual([item["kind"] for item in policy.snapshot()["pipeline"]["failures"]],
                         ["access_response", "harness_scope"])


class EvidenceTests(unittest.TestCase):
    def test_notes_accept_claim_levels_but_never_a_confirmed_verdict(self):
        notes = CandidateNotebook()
        self.assertTrue(notes.note_candidate("widget", evidence_level="controlled_comparison", observation_steps=[1, 2])["recorded"])
        self.assertEqual(notes.notes[0]["observation_steps"], [1, 2])
        for values in ({"evidence_level": "confirmed"}, {"observation_steps": [True]},
                       {"observation_steps": [0]}, {"observation_steps": [1] * 9}, {"evidence_level": []}):
            self.assertFalse(notes.note_candidate("bad", **values)["recorded"])

    def test_missing_failed_current_and_output_only_references_remain_unverified(self):
        policy = J2Policy()
        policy.record(ExecutionResult("E1", "run_python", exit_code=1, stdout="partial"), 1)
        policy.record(ExecutionResult("E2", "run_python", exit_code=0, stdout="normal output"), 2)
        notes = CandidateNotebook()
        notes.note_candidate("widget", evidence_level="security_effect", observation_steps=[1, 2, 3, 99])
        policy.record(ExecutionResult("E3", "run_python", exit_code=0, stdout="claim", investigation_notes=notes.notes), 3)
        candidate = policy.snapshot()["candidates"][0]
        self.assertEqual(candidate["evidence_level"], "security_effect")
        self.assertEqual(candidate["available_observation_steps"], [2])
        self.assertEqual(candidate["unavailable_observation_steps"], [1, 3, 99])
        self.assertEqual(candidate["verification"], "model_statement_only")

    def test_process_score_does_not_count_outputs_as_verified_findings(self):
        policy = J2Policy()
        policy.record(ExecutionResult("E1", "run_python", exit_code=0, stdout="arbitrary output"), 1)
        notes = CandidateNotebook()
        notes.note_candidate("widget", evidence_level="security_effect", observation_steps=[1])
        policy.record(ExecutionResult("E2", "run_python", exit_code=0, investigation_notes=notes.notes), 2)
        session = {"run_result": "completed", "findings": [{"status": "ok", "evidence": True, "observed": "output"}],
                   "j2_state": policy.snapshot()}
        with tempfile.TemporaryDirectory() as folder:
            scored = score_run(session, Path(folder))
            legacy = score_run({"findings": session["findings"]}, Path(folder))
        self.assertEqual(scored["execution_output_steps"], 1)
        self.assertEqual(scored["pipeline_metrics"]["claim_counts"]["security_effect"], 1)
        self.assertEqual(scored["pipeline_metrics"]["claims_with_available_references"], 1)
        self.assertIsNone(scored["pipeline_metrics"]["verified_findings"])
        self.assertIsNone(legacy["pipeline_metrics"])


    def test_claim_level_survives_updates_and_indicates_reference_availability_only(self):
        policy = J2Policy()
        for step in (1, 2):
            policy.record(ExecutionResult(str(step), "run_python", exit_code=0, stdout="output"), step)
        notes = CandidateNotebook()
        notes.note_candidate("widget", evidence_level="controlled_comparison", observation_steps=[1, 2])
        policy.record(ExecutionResult("E3", "run_python", exit_code=0, investigation_notes=notes.notes), 3)
        followup = CandidateNotebook()
        followup.note_candidate("widget", state="closed")
        policy.record(ExecutionResult("E4", "run_python", exit_code=0, investigation_notes=followup.notes), 4)
        candidate = policy.context()["candidates"][0]
        self.assertEqual(candidate["available_observation_steps"], [1, 2])
        self.assertEqual(candidate["evidence_level"], "controlled_comparison")
        self.assertEqual(candidate["verification"], "model_statement_only")


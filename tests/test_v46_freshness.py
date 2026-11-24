import copy
import json
import tempfile
import unittest
from pathlib import Path

from engine.session_log import SessionLog
from engine.tree import TreeStore, replay_tree
from engine.contracts import ContractError


def discovery(path, kind="surface", relation="discovered"):
    return {"kind": kind, "relation": relation,
            "locator": {"url": "https://example.test/" + path, "method": "GET", "input": "q", "auth_context": "anonymous"},
            "hypothesis": {"type": "sqli", "statement": "hypothesis"} if kind == "candidate" else None}


class FreshnessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.log = SessionLog(Path(self.temp.name), "a" * 32, {"experiment_mode": "v4"})
        self.addCleanup(self.log.close)
        self.tree = TreeStore(self.log)
        self.tree.initialize("https://example.test/")
        self.parent = self.tree.discover("N0001", discovery("a", "candidate"))
        self.child = self.tree.discover(self.parent, discovery("b", relation="refines"))
        self.tree.mark_seeded(self.parent, "b" * 32, "N0002", {"response_status": 200})
        self.tree.mark_seeded(self.child, "b" * 32, "N0003", {}, reused=True)

    def observe(self, status, *, url="https://example.test/a", exit_code=0, include_evidence=True, node_id=None, result_observations=None, method=None, auth=None, other_status=None):
        node_id = node_id or self.parent
        ordinal = len(self.tree.state["attempts"]) + 1
        call_id, exec_id = f"CALL-{ordinal}", f"EXEC-{ordinal}"
        attempt = self.tree.begin_attempt(node_id, call_id=call_id)
        self.log.append("exec_started", {"exec_id": exec_id, "call_id": call_id, "action": "run_command"})
        base = self.log.run_dir / "runtime"
        result_path = base / attempt["result_path"]
        result_path.parent.mkdir(parents=True, exist_ok=True)
        header = f"URL: {url}\nStatus: {status}\n"
        if method is not None:
            header += f"Method: {method}\n"
        if auth is not None:
            header += f"Auth-Context: {auth}\n"
        (base / "work" / "response.txt").write_text(header + "\nbody", encoding="utf-8")
        result = {"schema_version": 1, "attempt_id": attempt["attempt_id"], "node_id": node_id,
                  "discovered": [], "proposed_verdict": "confirmed", "claim": "claim", "next_observation": "",
                  "evidence_refs": [{"path": "work/response.txt", "description": "response"}] if include_evidence else []}
        if other_status is not None:
            (base / "work" / "other-response.txt").write_text(header.replace(f"Status: {status}", f"Status: {other_status}") + "\nbody", encoding="utf-8")
            result["evidence_refs"].append({"path": "work/other-response.txt", "description": "second response"})
        if result_observations is not None:
            result["observations"] = result_observations
        result_path.write_text(json.dumps(result), encoding="utf-8")
        self.log.append("exec_finished", {"exec_id": exec_id, "call_id": call_id, "action": "run_command",
                        "exit_code": exit_code, "timed_out": False, "cancelled": False, "failure_source": None})
        self.tree.import_result(attempt["attempt_id"], exec_id=exec_id)

    def test_dependency_revision_changes_on_freshness_and_revalidation_but_not_identical_results(self):
        initial = self.tree.state.get("seed_dependency_revision")
        self.assertIsNotNone(initial)
        self.observe(200)
        checked = self.tree.state["seed_dependency_revision"]
        self.assertGreater(checked, initial)
        self.observe(200)
        self.assertEqual(self.tree.state["seed_dependency_revision"], checked)
        # A repeated import delivery also leaves dependency state unchanged.
        events = self.log.snapshot_events()
        repeated = copy.deepcopy(events[-1])
        repeated["seq"] += 1
        self.assertEqual(replay_tree([*events, repeated], self.log.run_id)["seed_dependency_revision"], checked)
        self.observe(404)
        stale = self.tree.state["seed_dependency_revision"]
        self.assertGreater(stale, checked)
        self.assertTrue(self.tree.state["nodes"][self.child]["seed_revalidation_pending"])
        self.observe(200, node_id=self.child, url="https://example.test/b")
        self.assertFalse(self.tree.state["nodes"][self.child]["seed_revalidation_pending"])
        self.assertGreater(self.tree.state["seed_dependency_revision"], stale)
        committed = self.tree.state
        self.assertEqual(self.tree.rebuild(), committed)

    def test_dependency_revision_tracks_approval_replacement_and_failure(self):
        dependent = self.tree.discover(self.parent, discovery("cap", relation="enabled_by"))
        self.tree.mark_seeded(dependent, "b" * 32, "N0004", {})
        self.observe(200)
        initial = self.tree.state.get("seed_dependency_revision")
        self.assertIsNotNone(initial)
        ref = self.tree.state["attempts"]["A0001"]["evidence_refs"][0]
        self.tree.record_seed_dependency_verification(dependent, self.parent, "A0001", ref)
        approved = self.tree.state["seed_dependency_revision"]
        self.assertGreater(approved, initial)
        self.tree.record_seed_dependency_verification(dependent, self.parent, "A0001", ref)
        self.assertEqual(self.tree.state["seed_dependency_revision"], approved)
        self.observe(200)
        self.assertEqual(self.tree.state["seed_dependency_revision"], approved)
        ref = self.tree.state["attempts"]["A0002"]["evidence_refs"][0]
        self.tree.record_seed_dependency_verification(dependent, self.parent, "A0002", ref)
        replaced = self.tree.state["seed_dependency_revision"]
        self.assertGreater(replaced, approved)
        attempt = self.tree.begin_attempt(self.parent, call_id="CALL-failed")
        self.tree.fail_attempt(attempt["attempt_id"], reason="not_executed", exec_id=None)
        failed = self.tree.state["seed_dependency_revision"]
        self.assertGreater(failed, replaced)
        attempt = self.tree.begin_attempt(self.parent, call_id="CALL-failed-again")
        self.tree.fail_attempt(attempt["attempt_id"], reason="not_executed", exec_id=None)
        self.assertEqual(self.tree.rebuild()["seed_dependency_revision"], failed)

    def test_dependency_revision_tracks_approval_removal_with_an_independent_path(self):
        primary = self.tree.discover("N0001", discovery("primary", "candidate"))
        alternate = self.tree.discover("N0001", discovery("alternate", "candidate"))
        dependent = self.tree.discover(primary, discovery("cap", relation="enabled_by"))
        self.tree.discover(alternate, discovery("cap", relation="enabled_by"))
        for node_id in (primary, alternate, dependent):
            self.tree.mark_seeded(node_id, "b" * 32, node_id, {})
        for parent, attempt_id, path in ((primary, "A0001", "primary"), (alternate, "A0002", "alternate")):
            self.observe(200, node_id=parent, url="https://example.test/" + path)
            ref = self.tree.state["attempts"][attempt_id]["evidence_refs"][0]
            self.tree.record_seed_dependency_verification(dependent, parent, attempt_id, ref)
        approved = self.tree.state.get("seed_dependency_revision")
        self.assertIsNotNone(approved)
        attempt = self.tree.begin_attempt(primary, call_id="CALL-failed")
        self.tree.fail_attempt(attempt["attempt_id"], reason="not_executed", exec_id=None)
        state = self.tree.rebuild()
        self.assertGreater(state["seed_dependency_revision"], approved)
        self.assertEqual(set(state["nodes"][dependent]["seed_enabled_by_verified"]), {alternate})
        self.assertFalse(state["nodes"][dependent].get("seed_revalidation_pending", False))
        self.assertEqual(state["nodes"][primary]["seed_freshness"], "unknown")

    def test_status_change_is_recorded_and_invalidates_refinement(self):
        self.observe(404)
        state = self.tree.rebuild()
        self.assertEqual(state["nodes"][self.parent]["seed_freshness"], "stale")
        self.assertEqual(state["nodes"][self.child]["investigation_state"], "pending")
        self.assertTrue(state["nodes"][self.child]["seed_revalidation_pending"])
        self.assertEqual(state["nodes"][self.parent]["verification_state"], "inconclusive")

    def test_equal_status_checks_only_that_field(self):
        self.observe(200)
        node = self.tree.rebuild()["nodes"][self.parent]
        self.assertEqual(node["seed_freshness"], "checked")
        self.assertEqual(node["seed_checked_fields"], ["response_status"])

    def test_missing_matching_evidence_does_not_check(self):
        self.observe(200, url="https://other.test/a")
        self.assertEqual(self.tree.rebuild()["nodes"][self.parent]["seed_freshness"], "unknown")

    def test_failed_execution_does_not_check(self):
        self.observe(200, exit_code=1)
        self.assertEqual(self.tree.rebuild()["nodes"][self.parent]["seed_freshness"], "unknown")

    def test_operator_attests_only_enabled_by_edge_with_current_parent_evidence(self):
        dependent = self.tree.discover(self.parent, discovery("cap", relation="enabled_by"))
        self.tree.mark_seeded(dependent, "b" * 32, "N0004", {})
        self.observe(200)
        attempt = self.tree.state["attempts"]["A0001"]
        evidence = attempt["evidence_refs"][0]
        self.tree.record_seed_dependency_verification(dependent, self.parent, "A0001", evidence)
        node = self.tree.rebuild()["nodes"][dependent]
        self.assertEqual(node["seed_enabled_by_verified"][self.parent]["source"], "operator_reviewed")
        self.assertEqual(node["seed_enabled_by_verified"][self.parent]["attempt_id"], "A0001")
        with self.assertRaises(ContractError):
            self.tree.record_seed_dependency_verification(self.child, self.parent, "A0001", evidence)
        with self.assertRaises(ContractError):
            self.tree.record_seed_dependency_verification(dependent, self.parent, "A0001", {**evidence, "sha256": "0" * 64})
        self.observe(404)
        self.assertEqual(self.tree.rebuild()["nodes"][dependent]["seed_enabled_by_verified"], {})
        with self.assertRaises(ContractError):
            self.tree.record_seed_dependency_verification(dependent, self.parent, "A0001", evidence)
        self.observe(404)
        newer = self.tree.state["attempts"]["A0003"]["evidence_refs"][0]
        self.tree.record_seed_dependency_verification(dependent, self.parent, "A0003", newer)
        self.assertEqual(self.tree.rebuild()["nodes"][dependent]["seed_enabled_by_verified"][self.parent]["attempt_id"], "A0003")

    def test_failed_attempt_cannot_attest_dependency(self):
        dependent = self.tree.discover(self.parent, discovery("cap", relation="enabled_by"))
        self.tree.mark_seeded(dependent, "b" * 32, "N0004", {})
        self.observe(200, exit_code=1)
        evidence = self.tree.state["attempts"]["A0001"]["evidence_refs"][0]
        with self.assertRaises(ContractError):
            self.tree.record_seed_dependency_verification(dependent, self.parent, "A0001", evidence)

    def test_reversion_to_prior_invalidates_newer_dependency_review(self):
        dependent = self.tree.discover(self.parent, discovery("cap", relation="enabled_by"))
        self.tree.mark_seeded(dependent, "b" * 32, "N0004", {})
        self.observe(404)
        self.observe(404)
        ref = self.tree.state["attempts"]["A0002"]["evidence_refs"][0]
        self.tree.record_seed_dependency_verification(dependent, self.parent, "A0002", ref)
        self.observe(200)
        state = self.tree.rebuild()
        self.assertEqual(state["nodes"][self.parent]["seed_freshness"], "checked")
        self.assertEqual(state["nodes"][dependent]["seed_enabled_by_verified"], {})

    def test_invalidating_one_path_preserves_independent_approved_path_and_descendants(self):
        from strategies.greedy import seed_dispatchable
        alternate = self.tree.discover("N0001", discovery("alternate", "candidate"))
        dependent = self.tree.discover(self.parent, discovery("cap", "candidate", "enabled_by"))
        self.tree.discover(alternate, discovery("cap", "candidate", "enabled_by"))
        descendant = self.tree.discover(dependent, discovery("deep", "candidate", "enabled_by"))
        for node_id in (alternate, dependent, descendant):
            self.tree.mark_seeded(node_id, "b" * 32, node_id, {"response_status": 200})
        self.observe(200)
        self.observe(200, node_id=alternate, url="https://example.test/alternate")
        self.observe(200, node_id=dependent, url="https://example.test/cap")
        for child, parent, attempt_id in ((dependent, self.parent, "A0001"),
                                          (dependent, alternate, "A0002"),
                                          (descendant, dependent, "A0003")):
            ref = self.tree.state["attempts"][attempt_id]["evidence_refs"][0]
            self.tree.record_seed_dependency_verification(child, parent, attempt_id, ref)
        self.observe(404)
        state = self.tree.rebuild()
        self.assertNotIn(self.parent, state["nodes"][dependent]["seed_enabled_by_verified"])
        self.assertIn(alternate, state["nodes"][dependent]["seed_enabled_by_verified"])
        self.assertIn(dependent, state["nodes"][descendant]["seed_enabled_by_verified"])
        self.assertEqual(state["nodes"][dependent]["seed_freshness"], "checked")
        self.assertFalse(state["nodes"][dependent].get("seed_revalidation_pending", False))
        self.assertTrue(seed_dispatchable(state, descendant, "https://example.test/"))
        # An unrelated invalidation does not age the independent edge's evidence.
        independent_ref = state["attempts"]["A0002"]["evidence_refs"][0]
        self.tree.record_seed_dependency_verification(dependent, alternate, "A0002", independent_ref)
        old_ref = state["attempts"]["A0001"]["evidence_refs"][0]
        with self.assertRaises(ContractError):
            self.tree.record_seed_dependency_verification(dependent, self.parent, "A0001", old_ref)

    def test_alternate_parent_depending_on_invalidated_path_is_not_independent(self):
        from strategies.greedy import seed_dispatchable
        alternate = self.tree.discover(self.parent, discovery("alternate", "candidate", "refines"))
        dependent = self.tree.discover(self.parent, discovery("cap", "candidate", "enabled_by"))
        self.tree.discover(alternate, discovery("cap", "candidate", "enabled_by"))
        for node_id in (alternate, dependent):
            self.tree.mark_seeded(node_id, "b" * 32, node_id, {"response_status": 200})
        self.observe(200, node_id=alternate, url="https://example.test/alternate")
        ref = self.tree.state["attempts"]["A0001"]["evidence_refs"][0]
        self.tree.record_seed_dependency_verification(dependent, alternate, "A0001", ref)
        self.observe(404)
        state = self.tree.rebuild()
        self.assertEqual(state["nodes"][dependent]["seed_enabled_by_verified"], {})
        self.assertTrue(state["nodes"][dependent]["seed_revalidation_pending"])
        self.assertFalse(seed_dispatchable(state, dependent, "https://example.test/"))

    def test_discovery_provenance_does_not_make_alternate_capability_dependent(self):
        from strategies.greedy import seed_dispatchable
        alternate = self.tree.discover(self.parent, discovery("alternate", "candidate", "discovered"))
        dependent = self.tree.discover(self.parent, discovery("cap", "candidate", "enabled_by"))
        self.tree.discover(alternate, discovery("cap", "candidate", "enabled_by"))
        for node_id in (alternate, dependent):
            self.tree.mark_seeded(node_id, "b" * 32, node_id, {"response_status": 200})
        self.observe(200, node_id=alternate, url="https://example.test/alternate")
        ref = self.tree.state["attempts"]["A0001"]["evidence_refs"][0]
        self.tree.record_seed_dependency_verification(dependent, alternate, "A0001", ref)
        self.observe(404)
        state = self.tree.rebuild()
        self.assertIn(alternate, state["nodes"][dependent]["seed_enabled_by_verified"])
        self.assertFalse(state["nodes"][dependent].get("seed_revalidation_pending", False))
        self.assertTrue(seed_dispatchable(state, dependent, "https://example.test/"))

    def test_freshness_and_import_are_one_replayable_event(self):
        self.observe(404)
        events = self.log.snapshot_events()
        imported = next(e for e in events if e["kind"] == "node_result_imported")
        self.assertEqual(imported["payload"]["seed_freshness"]["response_status"], 404)
        self.assertFalse(any(e["kind"] == "node_seed_freshness_recorded" for e in events))
        self.assertEqual(self.tree.rebuild()["nodes"][self.child]["investigation_state"], "pending")

    def test_failed_import_does_not_replace_seed_observations_and_revokes_old_freshness(self):
        self.observe(200)
        self.observe(404, exit_code=1, result_observations={"response_status": 404})
        parent = self.tree.rebuild()["nodes"][self.parent]
        self.assertEqual(parent["observations"]["response_status"], 200)
        self.assertEqual(parent["seed_freshness"], "unknown")

    def test_failed_attempt_without_result_revokes_previous_freshness(self):
        self.observe(200)
        attempt = self.tree.begin_attempt(self.parent, call_id="CALL-failed")
        self.tree.fail_attempt(attempt["attempt_id"], reason="not_executed", exec_id=None)
        self.assertEqual(self.tree.rebuild()["nodes"][self.parent]["seed_freshness"], "unknown")

    def test_failed_attempt_revokes_attestation_even_if_freshness_unknown(self):
        parent = self.tree.discover("N0001", discovery("unknown", "candidate"))
        dependent = self.tree.discover(parent, discovery("child", relation="enabled_by"))
        self.tree.mark_seeded(parent, "b" * 32, "N0005", {})
        self.tree.mark_seeded(dependent, "b" * 32, "N0006", {})
        self.observe(200, node_id=parent, url="https://example.test/unknown")
        evidence = self.tree.state["attempts"]["A0001"]["evidence_refs"][0]
        self.tree.record_seed_dependency_verification(dependent, parent, "A0001", evidence)
        attempt = self.tree.begin_attempt(parent, call_id="CALL-failed")
        self.tree.fail_attempt(attempt["attempt_id"], reason="not_executed", exec_id=None)
        self.assertEqual(self.tree.rebuild()["nodes"][dependent]["seed_enabled_by_verified"], {})

    def test_revalidation_with_fresh_evidence_completes_without_claiming_prior_checked(self):
        self.observe(404)
        self.assertTrue(self.tree.state["nodes"][self.child]["seed_revalidation_pending"])
        self.observe(200, node_id=self.child, url="https://example.test/b")
        child = self.tree.rebuild()["nodes"][self.child]
        self.assertFalse(child["seed_revalidation_pending"])
        self.assertEqual(child["seed_freshness"], "unknown")

    def test_revalidation_ignores_unrelated_evidence(self):
        self.observe(404)
        self.observe(200, node_id=self.child, url="https://example.test/not-b")
        child = self.tree.rebuild()["nodes"][self.child]
        self.assertTrue(child["seed_revalidation_pending"])

    def test_revalidation_remains_pending_when_response_evidence_conflicts(self):
        self.observe(404)
        self.observe(200, node_id=self.child, url="https://example.test/b", other_status=404)
        self.assertTrue(self.tree.rebuild()["nodes"][self.child]["seed_revalidation_pending"])

    def test_authenticated_post_revalidation_needs_matching_method_and_context(self):
        item = discovery("submit", relation="refines")
        item["locator"].update(method="POST", auth_context="authenticated")
        child = self.tree.discover(self.parent, item)
        self.tree.mark_seeded(child, "b" * 32, "N0004", {})
        self.observe(404)
        self.observe(200, node_id=child, url="https://example.test/submit")
        self.assertTrue(self.tree.state["nodes"][child]["seed_revalidation_pending"])
        self.observe(200, node_id=child, url="https://example.test/submit", method="POST", auth="authenticated")
        node = self.tree.rebuild()["nodes"][child]
        self.assertFalse(node["seed_revalidation_pending"])
        self.assertEqual(node["seed_freshness"], "unknown")

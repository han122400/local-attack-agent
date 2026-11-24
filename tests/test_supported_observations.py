import hashlib
import json
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from engine.command_runtime import CommandRuntime
from engine.observation_verification import SHAPE_POLICY
from engine.reporting import save_report
from engine.risk_rubric import CRITERIA, fingerprint, project_node, validate_answer
from engine.session_log import LogFailure, SessionLog
from engine.tree import TreeStore, replay_tree
from engine.scorer_session import RiskScorer
from test_scorer_session import FakeAdapter, PIN
from test_v4_tree import surface


POLICY = "supported_observations_only"


class SupportedObservationsTests(unittest.TestCase):
    def use_shape_policy(self):
        self.log.update(scorer_settings={"reevaluation_policy": SHAPE_POLICY})

    def email_surface(self, path):
        discovery = surface(self.target + path + "?email=", "surface")
        discovery["locator"]["input"] = "email"
        return self.tree.discover(self.root, discovery)

    def setUp(self):
        class Handler(BaseHTTPRequestHandler):
            unstable_hits = 0

            def do_GET(self):
                parts = urlsplit(self.path)
                query = parse_qs(parts.query)
                count = 2 if query.get("q") == ["two"] else 0
                if parts.path == "/security-question":
                    body = json.dumps({"question": {"id": 1}} if query.get("email") == ["known@example.test"] else {}).encode()
                elif parts.path == "/echo-key":
                    body = json.dumps({query.get("email", [""])[0]: "reflected"}).encode()
                elif parts.path == "/constant-key":
                    body = json.dumps({"known@example.test": "constant"}).encode()
                elif parts.path == "/unstable":
                    if query.get("email") == ["known@example.test"]:
                        Handler.unstable_hits += 1
                        body = json.dumps({"question" if Handler.unstable_hits % 2 else "other": {}}).encode()
                    else:
                        body = b"{}"
                elif parts.path == "/shapes":
                    body = json.dumps({"question": "known"} if count else {}).encode()
                else:
                    body = json.dumps({"data": list(range(count)), "echo": self.path}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        self.target = f"http://127.0.0.1:{self.server.server_port}/"
        self.log = SessionLog(self.directory, "a" * 32, {
            "experiment_mode": "v4", "target": self.target,
            "scorer_provider": "openrouter", "scorer_model": PIN["id"],
            "scorer_settings": {"reevaluation_policy": POLICY}})
        self.addCleanup(self.log.close)
        self.tree = TreeStore(self.log)
        self.root = self.tree.initialize(self.target)
        self.node = self.tree.discover(self.root, surface(self.target + "items?q=", "candidate"))
        self.runtime = CommandRuntime(self.directory, self.log)
        self.runtime.start_session(self.log.run_id, target=self.target, browser=False)
        self.addCleanup(self.runtime.stop, self.log.run_id)

    def execute(self, test_value="two", capture=True, proofs=None, discoveries=None, import_result=True,
                check_locator=False, signal=None, parameter="q", baseline_value="zero", pointer="/data"):
        attempt = self.tree.begin_attempt(self.node, call_id=f"CALL-{len(self.tree.state['attempts'])}")
        value = {"schema_version": 1, "node_id": self.node, "attempt_id": attempt["attempt_id"],
                 "discovered": [], "proposed_verdict": "confirmed", "claim": "UNVERIFIED CLAIM",
                 "next_observation": "", "evidence_refs": [],
                 "observations": {"response_status": 200, "baseline_compared": True,
                                  "reproduced": True, "query_behavior": "controlled_difference"}}
        if discoveries:
            value["discovered"] = discoveries
        if signal:
            value["observations"]["security_signal"] = signal
        if proofs:
            value["observation_evidence"] = proofs
        code = "import json\nfrom pathlib import Path\n"
        code += f"result = {value!r}\n"
        if capture:
            locator = self.tree.state["nodes"][self.node]["locator"]
            url = locator["url"].split("?", 1)[0]
            if self.node == self.root:
                url = discoveries[0]["locator"]["url"].split("?", 1)[0] if discoveries else self.target + "items"
            code += "from http_capture_client import request\n"
            if parameter == "q" and baseline_value == "zero":
                code += f"base = [request({(url + '?q=zero')!r})['request_id'] for _ in range(2)]\n"
                code += f"test = [request({(url + '?q=' + test_value)!r})['request_id'] for _ in range(2)]\n"
            else:
                code += f"base = [request({url!r}, params={{{parameter!r}: {baseline_value!r}}})['request_id'] for _ in range(2)]\n"
                code += f"test = [request({url!r}, params={{{parameter!r}: {test_value!r}}})['request_id'] for _ in range(2)]\n"
            code += f"proof = {{'baseline': base, 'test': test, 'json_pointer': {pointer!r}}}\n"
            code += "result['observation_evidence'] = {k: proof for k in ('reproduced', 'query_behavior', 'baseline_compared')}\n"
            code += "result['observation_evidence']['response_status'] = {'request_ids': [test[-1]]}\n"
            if check_locator:
                code += f"exact = request({locator['url']!r})['request_id']\n"
                code += "result['observation_evidence']['response_status'] = {'request_ids': [exact]}\n"
            code += "for child in result['discovered']:\n    child['observations'] = result['observations']\n    child['observation_evidence'] = result['observation_evidence']\n"
        code += f"p = Path({attempt['result_path']!r})\np.parent.mkdir(parents=True, exist_ok=True)\np.write_text(json.dumps(result))\n"
        script = self.runtime.runtime_dir / "tools" / "fixture.py"
        script.write_text(code, encoding="utf-8")
        output = self.runtime.run(self.log.run_id, {"action": "run_command", "command": f'"{sys.executable}" "{script}"'},
                                  call_id=attempt["call_id"])
        self.assertEqual(output.exit_code, 0, output.stderr + output.exception)
        if not import_result:
            return attempt, output
        self.tree.import_result(attempt["attempt_id"], exec_id=output.exec_id)
        return self.tree.state["nodes"][self.node]

    def test_missing_capture_does_not_become_false_or_supported(self):
        node = self.execute(capture=False)
        projected = project_node(node, projection_version=POLICY)
        self.assertIsNone(projected["observations"]["reproduced"])
        self.assertEqual(projected["observations"]["query_behavior"], "unknown")
        self.assertEqual(node["observation_verification"]["fields"]["reproduced"]["state"], "unsupported")
        self.assertTrue(node["observations"]["reproduced"])

    def test_runtime_capture_supports_counts_and_survives_recovery_and_reporting(self):
        node = self.execute()
        self.assertTrue(project_node(node, projection_version=POLICY)["observations"]["reproduced"])
        self.assertEqual(node["observation_verification"]["fields"]["query_behavior"]["state"], "supported")
        captures = [e for e in self.log.snapshot_events() if e["kind"] == "http_exchange_recorded"]
        self.assertEqual(len(captures), 4)
        for event in captures:
            for side in ("request", "response"):
                ref = event["payload"][side]
                raw = (self.log.run_dir / ref["path"]).read_bytes()
                self.assertEqual(hashlib.sha256(raw).hexdigest(), ref["sha256"])
                self.assertEqual(ref["source"], "harness_http")
        scorer = RiskScorer(self.tree, adapter=FakeAdapter())
        scorer.score_children(self.root)
        request = self.tree.state["scorer"]["requests"]["S000001"]
        self.assertEqual(request["request"]["state"]["nodes"][self.node]["observations"]["query_behavior"], "controlled_difference")
        restored = TreeStore(self.log).rebuild()
        self.assertEqual(restored["scorer"], self.tree.state["scorer"])
        report = save_report(self.log, "incomplete", "stopped", state=restored)
        self.assertNotIn("UNVERIFIED CLAIM", json.dumps(report))
        self.assertEqual(report["candidate_leads"][0]["observations"]["response_status"], 200)

    def test_reflected_input_with_equal_zero_counts_is_contradicted(self):
        node = self.execute(test_value="alsozero")
        fields = node["observation_verification"]["fields"]
        self.assertEqual(fields["query_behavior"]["state"], "contradicted")
        self.assertEqual(fields["reproduced"]["state"], "contradicted")
        self.assertEqual(project_node(node, projection_version=POLICY)["observations"]["query_behavior"], "unknown")

    def test_changed_capture_is_rejected_before_import_and_on_recovery(self):
        attempt, output = self.execute(import_result=False)
        record = next(e["payload"] for e in self.log.snapshot_events() if e["kind"] == "http_exchange_recorded")
        (self.log.run_dir / record["response"]["path"]).write_bytes(b"tampered")
        with self.assertRaisesRegex(ValueError, "changed"):
            self.tree.import_result(attempt["attempt_id"], exec_id=output.exec_id)
        with self.assertRaises(LogFailure):
            TreeStore(self.log).rebuild()

    def test_old_attempt_cannot_support_new_claim_and_cache_is_recomputed(self):
        node = self.execute()
        scorer = RiskScorer(self.tree, adapter=FakeAdapter())
        first = scorer.score_children(self.root)
        proofs = self.tree.state["attempts"][node["attempts"][-1]]["result"]["observation_evidence"]
        updated = self.execute(capture=False, proofs=proofs)
        self.assertIsNone(project_node(updated, projection_version=POLICY)["observations"]["reproduced"])
        second = scorer.score_children(self.root)
        self.assertNotEqual(first["context_key"], second["context_key"])

    def test_verified_discovery_reaches_scorer_and_orders_ahead_of_unverified_sibling(self):
        unsupported = self.node
        self.tree.set_observations(unsupported, {"reproduced": True, "query_behavior": "controlled_difference"})
        self.node = self.root
        discovered = surface(self.target + "verified?q=", "candidate")
        self.execute(discoveries=[discovered])
        supported = self.tree.state["nodes"][self.root]["child_ids"][-1]

        class EvidenceAdapter(FakeAdapter):
            def evaluate(adapter, pin, parent, siblings, node_ids, **kwargs):
                output = super(EvidenceAdapter, adapter).evaluate(pin, parent, siblings, node_ids, **kwargs)
                for node in siblings:
                    projected = project_node(node, projection_version=POLICY)
                    index = 3 if projected["observations"].get("reproduced") is True else 1
                    output["scores"][node["id"]] = validate_answer({"type": "score", "score": index,
                        "probabilities": {str(i): float(i == index) for i in range(5)}, "confidence": 1,
                        "legend": {str(i): text for i, text in enumerate(CRITERIA)}})
                    if node["id"] == unsupported:
                        self.assertNotIn("reproduced", node["observations"])
                return output

        result = RiskScorer(self.tree, adapter=EvidenceAdapter()).score_children(self.root)
        self.assertEqual(result["ordered_node_ids"], [supported, unsupported])
        self.assertGreater(self.tree.state["nodes"][supported]["risk"]["score"], self.tree.state["nodes"][unsupported]["risk"]["score"])

    def test_tampered_capture_cannot_be_published_as_supported(self):
        self.execute()
        record = next(e["payload"] for e in self.log.snapshot_events() if e["kind"] == "http_exchange_recorded")
        (self.log.run_dir / record["response"]["path"]).write_bytes(b"tampered")
        with self.assertRaises(LogFailure):
            RiskScorer(self.tree, adapter=FakeAdapter()).score_children(self.root)
        report = save_report(self.log, "incomplete", "stopped", state=self.tree.state)
        self.assertEqual(report["status"], "degraded")
        self.assertIsNone(report["candidate_leads"])

    def test_seed_status_uses_current_capture_and_replays_after_runtime_cleanup(self):
        self.tree.mark_seeded(self.node, "b" * 32, "N0002", {"response_status": 404})
        node = self.execute(check_locator=True)
        self.assertEqual(node["seed_freshness"], "stale")
        self.assertEqual(node["seed_freshness_evidence"]["evidence_ref"]["source"], "harness_http")
        self.assertEqual(project_node(node, projection_version=POLICY)["observations"]["response_status"], 200)
        self.runtime.stop(self.log.run_id)
        self.runtime.destroy(self.log.run_id)
        restored = TreeStore(self.log).rebuild()
        report = save_report(self.log, "incomplete", "stopped", state=restored)
        self.assertEqual(report["http_audit"]["status"], "recorded")
        self.assertEqual(report["candidate_leads"][0]["observations"]["response_status"], 200)

    def test_capture_rejects_out_of_scope_request_without_recording_it(self):
        from engine.http_capture import CaptureSession
        from engine.http_capture_client import request
        from urllib.error import HTTPError

        attempt = self.tree.begin_attempt(self.node, call_id="scope-check")
        capture = CaptureSession(self.log, self.target, attempt, "EXEC-TEST", "anonymous", 2)
        self.addCleanup(capture.close)
        with self.assertRaises(HTTPError):
            request("http://different.invalid/", endpoint=capture.endpoint)
        self.assertFalse(any(e["kind"] == "http_exchange_recorded" for e in self.log.snapshot_events()))

    def test_params_replace_existing_query_value_for_sync_and_async_helpers(self):
        import asyncio
        from engine.http_capture import CaptureSession
        from engine.http_capture_client import async_helpers, request

        attempt = self.tree.begin_attempt(self.node, call_id="params-check")
        self.log.append("exec_started", {"exec_id": "EXEC-PARAMS", "call_id": attempt["call_id"], "action": "run_python"})
        capture = CaptureSession(self.log, self.target, attempt, "EXEC-PARAMS", "anonymous", 5)
        self.addCleanup(capture.close)
        sync = request(self.target + "items?q=", params={"q": "apple"}, endpoint=capture.endpoint)
        asynchronous = asyncio.run(async_helpers(capture.endpoint)["audit_http"](
            "/items?q=", params={"q": "apple"}))
        self.assertEqual(sync["json"]["echo"], "/items?q=apple")
        self.assertEqual(asynchronous["json"]["echo"], "/items?q=apple")
        records = self.tree.refresh()["http_exchanges"]
        self.assertEqual(len(records), 2)
        for response in (sync, asynchronous):
            self.assertEqual(urlsplit(records[response["request_id"]]["facts"]["url"]).query, "q=apple")

    def test_command_helper_outside_capture_has_actionable_error(self):
        from unittest.mock import patch
        from engine.http_capture_client import request
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "run_python.*audit_http"):
                request(self.target)

    def test_unsupported_surface_signal_creates_queued_verification_candidate(self):
        self.node = self.tree.discover(self.root, surface(self.target + "shapes?q=", "surface"))
        node = self.execute(test_value="two", signal="account_enumeration")
        self.assertEqual(node["observation_verification"]["fields"]["query_behavior"]["state"], "unsupported")
        self.assertEqual(node["observation_verification"]["fields"]["security_signal"]["state"], "unsupported")
        lead_node = next(item for item in self.tree.state["nodes"].values()
                         if item.get("origin") == "unverified_lead")
        self.assertEqual(lead_node["parent_id"], self.node)
        self.assertEqual(lead_node["source_attempt_id"], node["attempts"][-1])
        self.assertEqual(lead_node["hypothesis"]["type"], "auth")
        self.assertEqual(lead_node["observations"]["query_behavior"], "unknown")
        self.assertEqual(lead_node["observations"].get("security_signal", "unknown"), "unknown")
        self.assertEqual(project_node(lead_node, projection_version=POLICY)["observations"]["query_behavior"], "unknown")
        self.assertEqual(project_node(lead_node, projection_version=POLICY)["observations"].get("security_signal", "unknown"), "unknown")
        self.assertEqual(len(lead_node["capture_request_ids"]), 4)
        self.assertEqual(TreeStore(self.log).rebuild()["nodes"][lead_node["id"]], lead_node)
        self.assertTrue(any(event["kind"] == "node_result_imported"
                            and event["payload"].get("unverified_lead_policy")
                            for event in self.log.snapshot_events()))
        RiskScorer(self.tree, adapter=FakeAdapter()).score_children(self.node)
        self.assertEqual(self.tree.state["nodes"][lead_node["id"]]["risk"]["status"], "scored")
        report = save_report(self.log, "completed", "tree_exhausted", state=self.tree.state)
        lead = next(item for item in report["unverified_leads"] if item["node_id"] == lead_node["id"])
        self.assertEqual(lead["verification_state"], "inconclusive")
        self.assertEqual(lead["followup_status"], "queued")
        self.assertTrue(lead["capture_request_ids"])
        self.assertTrue(lead["claim_refs"])
        self.tree.select_node(lead_node["id"])
        active_report = save_report(self.log, "incomplete", "stopped", state=self.tree.state)
        self.assertEqual(active_report["unverified_leads"][0]["followup_status"], "investigated")
        self.assertEqual(project_node(node, projection_version=POLICY)["observations"]["query_behavior"], "unknown")
        self.assertEqual(project_node(node, projection_version=POLICY)["observations"]["security_signal"], "unknown")
        from engine.http_capture import response_body
        bodies = []
        for record in self.tree.state["http_exchanges"].values():
            raw = (self.log.run_dir / record["response"]["path"]).read_bytes()
            bodies.append(json.loads(response_body(raw, "GET")[1]))
        self.assertEqual(bodies, [{}, {}, {"question": "known"}, {"question": "known"}])

    def test_shape_v2_supports_repeated_security_question_object_difference_in_score(self):
        self.use_shape_policy()
        self.node = self.email_surface("security-question")
        node = self.execute(parameter="email", baseline_value="missing@example.test",
                            test_value="known@example.test", pointer="", signal="account_enumeration")
        fields = node["observation_verification"]["fields"]
        self.assertEqual(fields["query_behavior"]["state"], "supported")
        self.assertEqual(fields["reproduced"]["state"], "supported")
        self.assertEqual(fields["security_signal"]["state"], "unsupported")
        self.assertEqual(sum(item.get("origin") == "unverified_lead"
                             for item in self.tree.state["nodes"].values()), 1)
        records = list(self.tree.state["http_exchanges"].values())
        self.assertEqual(len(records), 4)
        self.assertTrue(all(record["facts_schema"] == 2 for record in records))
        self.assertEqual([record["facts"]["json_shapes"][""]["keys"] for record in records],
                         [[], [], ["question"], ["question"]])
        from engine.http_capture import check_capture_records
        import copy
        forged = copy.deepcopy(records[2])
        forged["facts"]["json_shapes"][""]["keys"] = []
        with self.assertRaisesRegex(ValueError, "measurements changed"):
            check_capture_records(self.log.run_dir, [forged])
        self.assertEqual(project_node(node, projection_version=SHAPE_POLICY)["observations"]["query_behavior"],
                         "controlled_difference")
        RiskScorer(self.tree, adapter=FakeAdapter()).score_children(self.root)
        scored = self.tree.state["nodes"][self.node]
        self.assertEqual(scored["risk"]["status"], "scored")
        request = self.tree.state["scorer"]["requests"]["S000001"]["request"]
        self.assertEqual(request["state"]["nodes"][self.node]["observations"]["query_behavior"],
                         "controlled_difference")
        self.assertNotIn("security_signal", request["state"]["nodes"][self.node]["observations"])
        self.assertEqual(TreeStore(self.log).rebuild()["nodes"][self.node]["risk"], scored["risk"])
        report = save_report(self.log, "incomplete", "stopped", state=self.tree.state)
        self.assertEqual(report["observation_policy"], SHAPE_POLICY)
        self.assertEqual(report["http_audit"]["status"], "recorded")
        self.assertEqual(len(report["unverified_leads"]), 1)
        self.assertEqual(report["unverified_leads"][0]["followup_status"], "queued")

    def test_shape_v2_ignores_reflected_input_as_object_key(self):
        self.use_shape_policy()
        self.node = self.email_surface("echo-key")
        node = self.execute(parameter="email", baseline_value="missing@example.test",
                            test_value="known@example.test", pointer="")
        self.assertNotEqual(node["observation_verification"]["fields"]["query_behavior"]["state"], "supported")
        self.assertEqual(project_node(node, projection_version=SHAPE_POLICY)["observations"]["query_behavior"],
                         "unknown")

    def test_shape_v2_reflection_filter_cannot_invent_a_difference_from_a_static_key(self):
        self.use_shape_policy()
        self.node = self.email_surface("constant-key")
        node = self.execute(parameter="email", baseline_value="missing@example.test",
                            test_value="known@example.test", pointer="")
        self.assertNotEqual(node["observation_verification"]["fields"]["query_behavior"]["state"], "supported")

    def test_shape_v2_rejects_inconsistent_test_pair(self):
        self.use_shape_policy()
        self.node = self.email_surface("unstable")
        node = self.execute(parameter="email", baseline_value="missing@example.test",
                            test_value="known@example.test", pointer="")
        self.assertEqual(node["observation_verification"]["fields"]["query_behavior"]["state"], "unsupported")
        self.assertEqual(project_node(node, projection_version=SHAPE_POLICY)["observations"]["query_behavior"],
                         "unknown")

    def test_shape_v2_keeps_array_length_comparison(self):
        self.use_shape_policy()
        node = self.execute()
        self.assertEqual(node["observation_verification"]["fields"]["query_behavior"]["state"], "supported")
        self.assertEqual(project_node(node, projection_version=SHAPE_POLICY)["observations"]["query_behavior"],
                         "controlled_difference")

    def test_shape_v2_bounds_keys_depth_and_omits_scalar_values(self):
        from engine.http_capture import exchange_facts

        request = b"GET /items?q=needle HTTP/1.1\r\nHost: example.test\r\n\r\n"
        def measure(value):
            body = json.dumps(value).encode()
            response = (b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                        + str(len(body)).encode() + b"\r\n\r\n" + body)
            return exchange_facts(request, response, "https://example.test/items?q=needle", "anonymous",
                                  facts_schema=2)["json_shapes"]
        self.assertNotIn("secret-value", json.dumps(measure({"stable": "secret-value"})))
        self.assertEqual(measure({"needle": "reflected"})[""]["keys"], ["needle"])
        self.assertEqual(measure(None)[""]["type"], "null")
        self.assertEqual(measure([1])[""]["type"], "array")
        self.assertEqual(measure(3)[""]["type"], "scalar")
        self.assertEqual(measure({f"key{i}": i for i in range(33)}), {})
        wide = {f"g{i:02d}": {f"k{j:02d}" + "x" * 110: j for j in range(7)} for i in range(12)}
        self.assertEqual(measure(wide), {})
        deep = {}
        current = deep
        for _ in range(10):
            current["child"] = {}
            current = current["child"]
        self.assertEqual(measure(deep), {})

    def test_historical_capture_journal_keeps_v1_facts_hashes_and_scores(self):
        from engine.http_capture import check_capture_records

        directory = Path(__file__).resolve().parents[1] / "runs/1c7a795ddacc493c8fc714b8a2aaeb64"
        if not directory.exists():
            self.skipTest("Historical captured run is unavailable")
        events = [json.loads(line) for line in (directory / "events.jsonl").read_bytes().splitlines()]
        state = replay_tree(events, directory.name)
        records = list(state["http_exchanges"].values())
        self.assertEqual(len(records), 24)
        self.assertTrue(all("facts_schema" not in record and "json_shapes" not in record["facts"]
                            for record in records))
        check_capture_records(directory, records)
        starts = [event["payload"] for event in events if event["kind"] == "scorer_request_started"]
        self.assertTrue(starts)
        for original in starts:
            replayed = state["scorer"]["requests"][original["request_id"]]
            self.assertEqual(replayed["context_key"], original["context_key"])
            self.assertEqual(fingerprint(replayed["request"]), original["input_hash"])
        for node_id, node in state["nodes"].items():
            saved = json.loads((directory / "nodes" / f"{node_id}.json").read_bytes())
            self.assertEqual(node["risk"], saved["risk"])

    def test_duplicate_surface_claim_does_not_requeue_same_lead(self):
        self.node = self.tree.discover(self.root, surface(self.target + "shapes?q=", "surface"))
        self.execute(signal="account_enumeration")
        self.execute(signal="account_enumeration")
        self.assertEqual(sum(node.get("origin") == "unverified_lead"
                             for node in self.tree.state["nodes"].values()), 1)

    def test_unverified_query_difference_alone_creates_structural_lead(self):
        self.node = self.tree.discover(self.root, surface(self.target + "shapes?q=", "surface"))
        self.execute()
        lead = next(node for node in self.tree.state["nodes"].values()
                    if node.get("origin") == "unverified_lead")
        self.assertEqual(lead["hypothesis"]["type"], "input_behavior")
        self.assertEqual(lead["unverified_signal"], "controlled_difference")
        self.assertEqual(lead["observations"]["query_behavior"], "unknown")
        self.assertEqual(project_node(lead, projection_version=POLICY)["observations"]["query_behavior"], "unknown")

    def test_reinvestigated_lead_can_support_measured_behavior_without_promoting_claim(self):
        self.node = self.tree.discover(self.root, surface(self.target + "items?q=", "surface"))
        self.execute(signal="account_enumeration")
        self.node = next(node["id"] for node in self.tree.state["nodes"].values()
                         if node.get("origin") == "unverified_lead")
        lead = self.execute()
        self.assertEqual(lead["observation_verification"]["fields"]["query_behavior"]["state"], "supported")
        projected = project_node(lead, projection_version=POLICY)["observations"]
        self.assertEqual(projected["query_behavior"], "controlled_difference")
        self.assertEqual(projected.get("security_signal", "unknown"), "unknown")
        self.assertEqual(lead["verification_state"], "inconclusive")
        self.assertEqual(sum(node.get("origin") == "unverified_lead"
                             for node in self.tree.state["nodes"].values()), 1)

    def test_unverified_lead_run_cap_is_enforced_at_import(self):
        from unittest.mock import patch

        with patch("engine.tree.MAX_UNVERIFIED_LEADS_PER_RUN", 1):
            self.node = self.tree.discover(self.root, surface(self.target + "shapes?q=", "surface"))
            self.execute(signal="account_enumeration")
            self.node = self.tree.discover(self.root, surface(self.target + "other?q=", "surface"))
            self.execute(signal="account_enumeration")
        self.assertEqual(sum(node.get("origin") == "unverified_lead"
                             for node in self.tree.state["nodes"].values()), 1)

    def test_post_request_bytes_and_auth_mismatch_do_not_support_wrong_locator(self):
        from engine.http_capture import exchange_facts
        from engine.observation_verification import verify_observations

        request = b"POST /items?q= HTTP/1.1\r\nHost: example.test\r\nCookie: session=fixture\r\nContent-Length: 3\r\n\r\na=1"
        response = b"HTTP/1.1 201 Created\r\nContent-Length: 2\r\n\r\n{}"
        facts = exchange_facts(request, response, "https://example.test/items?q=", "anonymous")
        self.assertEqual(facts["method"], "POST")
        self.assertEqual(facts["auth_context"], "unknown")
        identity = "R" + "a" * 32
        value = {"observations": {"response_status": 201},
                 "observation_evidence": {"response_status": {"request_ids": [identity]}}}
        locator = {"url": facts["url"], "method": "POST", "auth_context": "anonymous", "input": "q"}
        result = verify_observations(value, locator, {identity: {"facts": facts}})
        self.assertEqual(result["fields"]["response_status"]["state"], "unsupported")

    def test_async_http_helpers_preserve_their_result_contract_without_a_browser(self):
        import asyncio
        from engine.http_capture import CaptureSession
        from engine.http_capture_client import async_helpers

        attempt = self.tree.begin_attempt(self.node, call_id="helper-check")
        self.log.append("exec_started", {"exec_id": "EXEC-HELPER", "call_id": attempt["call_id"], "action": "run_python"})
        capture = CaptureSession(self.log, self.target, attempt, "EXEC-HELPER", "anonymous", 5)
        self.addCleanup(capture.close)
        helpers = async_helpers(capture.endpoint)

        async def exercise():
            url = self.target + "items?q=two"
            result = await helpers["http_get"](url)
            self.assertEqual(result["status"], 200)
            self.assertEqual(result["json"]["data"], [0, 1])
            self.assertEqual(result["length"], len(result["body"]))
            self.assertNotIn("body_bytes", result)
            self.assertTrue(result["request_id"].startswith("R"))
            self.assertEqual((await helpers["get_json"](url))["data"], [0, 1])
            self.assertEqual(json.loads(await helpers["get_text"](url))["data"], [0, 1])

        asyncio.run(exercise())
        self.assertEqual(len(self.tree.refresh()["http_exchanges"]), 3)

    def test_existing_journal_keeps_recorded_inputs_context_and_scores(self):
        directory = Path(__file__).resolve().parents[1] / "runs/eaf6b51abde945a8bc10ecdf9583ce6b"
        if not directory.exists():
            self.skipTest("Historical local run is unavailable")
        events = [json.loads(line) for line in (directory / "events.jsonl").read_bytes().splitlines()]
        state = replay_tree(events, directory.name)
        self.assertEqual(state["scorer"]["configuration"]["projection_version"], "scoring_fields_v1")
        starts = [e["payload"] for e in events if e["kind"] == "scorer_request_started"]
        self.assertTrue(starts)
        for original in starts:
            replayed = state["scorer"]["requests"][original["request_id"]]
            self.assertEqual(replayed["context_key"], original["context_key"])
            self.assertEqual(fingerprint(replayed["request"]), original["input_hash"])
        for key, node in state["nodes"].items():
            saved = json.loads((directory / "nodes" / f"{key}.json").read_bytes())
            self.assertEqual(node["risk"], saved["risk"])

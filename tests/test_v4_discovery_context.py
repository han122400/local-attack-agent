import hashlib
import json
import unittest

import test_v4_loop as loop_fixture
from v4_loop import V4Runner


class DiscoveryContextTests(unittest.TestCase):
    def setUp(self):
        self.fixture = loop_fixture.LoopTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.log = self.fixture.log
        self.runtime = loop_fixture.Runtime(self.log)
        self.runner = V4Runner(self.log, self.runtime, scorer_adapter=loop_fixture.FakeAdapter())

    def record(self, text, path="work/response.txt", followup="Analyze the saved response", node_id="N0001"):
        number = len(self.runner.tree.state["attempts"]) + 1
        call_id, exec_id = f"CALL-{number:04d}", f"EXEC-{number:04d}"
        attempt = self.runner.tree.begin_attempt(node_id, call_id=call_id)
        self.log.append("exec_started", {"exec_id": exec_id, "call_id": call_id, "action": "run_command"})
        target = self.runtime.runtime_dir / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        result = {"schema_version": 1, "attempt_id": attempt["attempt_id"], "node_id": node_id,
                  "discovered": [], "proposed_verdict": "inconclusive", "claim": "",
                  "evidence_refs": [{"path": path, "description": "observed response"}],
                  "next_observation": followup}
        result_path = self.runtime.runtime_dir / attempt["result_path"]
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(json.dumps(result), encoding="utf-8")
        self.log.append("exec_finished", {"exec_id": exec_id, "call_id": call_id, "action": "run_command",
            "exit_code": 0, "timed_out": False, "cancelled": False, "failure_source": None})
        return self.runner.tree.import_result(attempt["attempt_id"], exec_id=exec_id)

    def test_result_template_requests_unknown_observations_without_copying_prior_facts(self):
        self.runner.tree.set_observations("N0001", {"input_control": "full", "response_status": 200})
        context = self.runner._context("N0001")
        template = context["result_template"].get("observations", {})
        self.assertEqual(template.get("input_control"), "unknown")
        self.assertIsNone(template.get("response_status"))
        self.assertEqual(context["node"]["observations"]["response_status"], 200)

    def test_long_html_is_analyzed_from_preserved_evidence_with_provenance_and_no_node_creation(self):
        body = ('URL: https://example.test/app/page\nContent-Type: text/html\n\n'
                '<html><style>' + 'x' * 20000 + '</style><script src="/assets/entry.js"></script>'
                '<form action="/api/search"><input name="q"></form></html>')
        self.record(body)
        digest = hashlib.sha256((self.runtime.runtime_dir / "work/response.txt").read_bytes()).hexdigest()
        (self.runtime.runtime_dir / "work/response.txt").unlink()
        context = self.runner._context("N0001")
        discovery = context.get("discovery_context", {})
        self.assertIn("https://example.test/assets/entry.js", [item["url"] for item in discovery.get("assets", [])])
        self.assertFalse(next(item for item in discovery["assets"] if item["url"] == "https://example.test/assets/entry.js")["fetched"])
        sources = discovery["sources"]
        self.assertEqual(sources[0]["evidence_path"], "work/response.txt")
        self.assertEqual(sources[0]["source_url"], "https://example.test/app/page")
        self.assertFalse(sources[0]["source_url_inferred"])
        self.assertEqual(sources[0]["sha256"], digest)
        full_path = self.runtime.runtime_dir / discovery["full_analysis_path"]
        full = json.loads(full_path.read_text(encoding="utf-8"))
        self.assertIn("https://example.test/assets/entry.js", [item["url"] for item in full["assets"]])
        self.assertEqual(len(self.runner.tree.state["nodes"]), 1)

    def test_explicit_external_source_is_not_resolved_against_the_authorized_origin(self):
        self.record('URL: https://outside.test/page\nContent-Type: text/html\n\n'
                    '<script src="/assets/external.js"></script><form action="/steal"><input name="token"></form>')
        context = self.runner._context("N0001")
        discovery = context.get("discovery_context")
        self.assertIsInstance(discovery, dict)
        self.assertEqual(discovery["assets"], [])
        self.assertEqual(discovery["surfaces"], [])

    def test_javascript_and_html_evidence_are_accumulated_using_their_observed_source_urls(self):
        self.record('GET https://example.test/app/page\n\n<script src="bundle.js"></script>')
        self.record('URL: https://example.test/app/bundle.js\n\nfetch("/api/items?q=")', path="work/bundle.js")
        discovery = self.runner._context("N0001").get("discovery_context", {})
        self.assertIn("https://example.test/app/bundle.js", [item["url"] for item in discovery.get("assets", [])])
        fetched = next(item for item in discovery["assets"] if item["url"] == "https://example.test/app/bundle.js")
        self.assertTrue(fetched["fetched"])
        self.assertFalse(fetched["fetched_inferred"])
        self.assertEqual(fetched["evidence_path"], "work/bundle.js")
        self.assertIn("https://example.test/api/items?q=", [item["locator"]["url"] for item in discovery["surfaces"]])
        self.assertEqual({item["source_url"] for item in discovery["sources"]},
                         {"https://example.test/app/page", "https://example.test/app/bundle.js"})

    def test_unique_asset_filename_matches_preserved_file_without_a_response_header(self):
        self.record('URL: https://example.test/app/page\nContent-Type: text/html\n\n'
                    '<script src="/assets/main.js"></script>')
        self.record('import{x}from"./chunk.js"; fetch("/api/search?q=");', path="work/main.js")
        discovery = self.runner._context("N0001")["discovery_context"]
        asset = next(item for item in discovery["assets"] if item["url"] == "https://example.test/assets/main.js")
        self.assertTrue(asset["fetched"])
        self.assertTrue(asset["fetched_inferred"])
        self.assertEqual(asset["evidence_path"], "work/main.js")

    def test_bundle_saved_as_plain_text_still_exposes_declared_input_hints(self):
        self.record('import{x}from"./chunk.js";this.http.get(server+`/v2/lookup?term=${x}`);',
                    path="work/main_js.txt")
        discovery = self.runner._context("N0001")["discovery_context"]
        self.assertTrue(discovery["sources"][0]["source_url_inferred"])
        self.assertTrue(any(item.get("path_hint") == "/v2/lookup?term=${x}" and
                            "term" in item["inputs"] for item in discovery["hints"]))
        self.assertEqual(len(self.runner.tree.state["nodes"]), 1)

    def test_context_compaction_keeps_followup_and_full_analysis_file(self):
        followup = "Analyze the module graph and concrete inputs" * 12
        body = '<html>' + ''.join(f'<script src="/assets/entry-{i}.js"></script>'
            f'<form action="/api/items-{i}"><input name="query_{i}"></form>' for i in range(90)) + '</html>'
        self.record(body, followup=followup)
        base = self.runner._context("N0001")
        self.runner.context_budget = len(json.dumps({**base, "discovery_context": {}}, ensure_ascii=False)) + 1700
        context = self.runner._context("N0001")
        discovery = context.get("discovery_context", {})
        self.assertTrue(discovery.get("truncated"))
        self.assertEqual(context["previous_result"]["next_observation"], followup)
        self.assertLessEqual(len(json.dumps(context, ensure_ascii=False)), self.runner.context_budget)
        full = json.loads((self.runtime.runtime_dir / discovery["full_analysis_path"]).read_text(encoding="utf-8"))
        self.assertGreater(len(full["assets"]), len(discovery["assets"]))
        self.assertEqual(len(self.runner.tree.state["nodes"]), 1)

    def test_source_limit_keeps_recent_observations_available(self):
        for index in range(17):
            self.record(f'URL: https://example.test/page-{index}\n\n<script src="/entry-{index}.js"></script>',
                        path=f"work/page-{index}.txt")
        discovery = self.runner._context("N0001")["discovery_context"]
        self.assertIn("work/page-16.txt", [source["evidence_path"] for source in discovery["sources"]])
        self.assertTrue(discovery["truncated"])

    def test_identical_evidence_hashes_keep_each_sources_relative_url_base(self):
        body = '<html><script src="entry.js"></script></html>'
        first = self.record(body, path="work/first.txt")
        first_context = self.runner._context("N0001")["discovery_context"]
        child = self.runner.tree.discover("N0001", loop_fixture.surface("https://example.test/nested/"))
        second = self.record(body, path="work/second.txt", node_id=child)
        self.assertEqual(first["evidence_refs"][0]["sha256"], second["evidence_refs"][0]["sha256"])
        second_context = self.runner._context(child)["discovery_context"]
        self.assertEqual(first_context["sources"][0]["source_url"], "https://example.test/")
        self.assertEqual(second_context["sources"][0]["source_url"], "https://example.test/nested/")
        self.assertIn("https://example.test/entry.js", [item["url"] for item in first_context["assets"]])
        self.assertIn("https://example.test/nested/entry.js", [item["url"] for item in second_context["assets"]])


if __name__ == "__main__":
    unittest.main()

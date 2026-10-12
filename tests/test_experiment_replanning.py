"""Regression tests for repeated experiment feedback and available request choices."""

import asyncio
import io
import json
import unittest

import explorer as explorer_module
from model_transport import mock_model
from test_planner import observed_run


def add_request(explorer, original, url):
    request = explorer._record(0, "requests", {
        "action_id": original["action_id"], "source": "browser", "method": "GET",
        "url": url, "query": explorer_module.query_fields(url)})
    response = explorer._record(0, "responses", {
        "request_id": request["id"], "status": 200, "content_type": "application/json",
        **explorer_module.summarize_body(b'{"items":[{"name":"Cocoa"}]}', "application/json")})
    return request, response


def add_screen_controls(explorer, screen):
    for kind, label, allowed in (("button", "Inspect catalog", ["click"]),
                                  ("input", "Catalog search", ["search"])):
        element = explorer._record(0, "elements", {
            "screen_id": screen["id"], "element_key": label, "kind": kind, "label": label,
            "type": "search" if kind == "input" else "button", "value": "",
            "visible": True, "allowed_actions": allowed})
        screen["element_ids"].append(element["id"])


class ExperimentReplanningTests(unittest.TestCase):
    def test_rejected_malformed_baseline_does_not_break_next_plan(self):
        explorer, screen, request, _ = observed_run()
        explorer.state["hypotheses"].append({"id": "HYP-001", "baseline": None,
                                             "change": {"query_key": "query", "value": "Pear"},
                                             "executable": False, "error": "Invalid baseline"})
        self.assertEqual([r["id"] for r in explorer._plan_context(screen)["requests"]], [request["id"]])

    def test_hypothesis_prompt_includes_prior_changes_for_same_request_url(self):
        # Regresses repeated nested LLM values caused by omitting rejected hypothesis feedback.
        explorer, _, request, response = observed_run()
        older, _ = add_request(explorer, request, request["url"])
        other, _ = add_request(explorer, request, explorer_module.TARGET + "catalog?query=Cocoa")
        explorer.state["hypotheses"] = [
            {"id": "HYP-001", "baseline": {"request_id": older["id"]},
             "change": {"query_key": "query", "value": "Banana"}, "executable": True, "error": None},
            {"id": "HYP-002", "baseline": {"request_id": request["id"]},
             "change": {"query_key": "query", "value": "Pear"}, "executable": False, "error": "Rejected proposal"},
            {"id": "HYP-003", "baseline": {"request_id": other["id"]},
             "change": {"query_key": "query", "value": "Cocoa"}, "executable": True, "error": None},
        ]
        stream = io.BytesIO(b'{"message":{"content":"{}"}}\n')
        with mock_model(return_value=stream) as opened:
            asyncio.run(explorer._ask_ollama(0, request, response))
        payload = json.loads(opened.call_args.args[0].content)
        prompt = json.loads(payload["messages"][1]["content"])
        self.assertCountEqual(prompt.get("previous_changes", []), [
            {"query_key": "query", "value": "Banana"},
            {"query_key": "query", "value": "Pear"},
        ])

    def test_exhausted_experiment_budget_removes_requests_but_keeps_screen_tools(self):
        # Regresses offering experiment requests that can only fail budget validation.
        explorer, screen, request, _ = observed_run()
        add_screen_controls(explorer, screen)
        explorer.state["hypotheses"] = [
            {"id": f"HYP-{number:03d}", "baseline": {"request_id": request["id"]},
             "change": {"query_key": "query", "value": str(number)}, "executable": True}
            for number in range(1, explorer_module.MAX_HYPOTHESES + 1)
        ]
        prompt = explorer._plan_context(screen)
        self.assertEqual(prompt["requests"], [])
        blocked = [item for item in prompt.get("blocked_actions", [])
                   if item.get("action") == "experiment" and item.get("request_id") == request["id"]]
        self.assertTrue(blocked, "Budget-exhausted requests need explicit planner feedback")
        self.assertTrue(blocked[0].get("reason"))
        self.assertEqual({action for element in prompt["elements"] for action in element["allowed_actions"]},
                         {"click", "search"})

    def test_failed_experiment_request_is_suppressed_only_in_its_ui_state(self):
        # Regresses reoffering a failed request while preserving other LLM-selected options.
        explorer, screen, request, _ = observed_run()
        screen["state_key"] = "catalog-view"
        other, _ = add_request(explorer, request, explorer_module.TARGET + "catalog?query=Cocoa")
        add_screen_controls(explorer, screen)
        explorer.state["decisions"].append({
            "id": "DEC-001", "screen_id": screen["id"], "state_key": "catalog-view",
            "request_key": request["url"], "element_key": "", "status": "failed", "error": "Duplicate hypothesis value",
            "answer": {"plan": ["조회 결과 비교"], "reason": "관찰된 요청 검증", "action": "experiment",
                       "request_id": request["id"], "element_id": "", "value": ""},
        })
        prompt = explorer._plan_context(screen)
        self.assertEqual([item["id"] for item in prompt["requests"]], [other["id"]])
        blocked = [item for item in prompt.get("blocked_actions", [])
                   if item.get("action") == "experiment" and item.get("request_id") == request["id"]]
        self.assertTrue(blocked)
        self.assertTrue(blocked[0].get("reason"))
        self.assertEqual({action for element in prompt["elements"] for action in element["allowed_actions"]},
                         {"click", "search"})
        changed = explorer._plan_context(dict(screen, state_key="catalog-filtered"))
        self.assertEqual({item["id"] for item in changed["requests"]}, {request["id"], other["id"]})


if __name__ == "__main__":
    unittest.main()

import io
import json
import time
import unittest
import asyncio
from unittest.mock import patch

import explorer as explorer_module
import httpx2 as httpx
from model_transport import mock_model
from planning import validate_decision


def observed_run():
    explorer = explorer_module.V2Explorer()
    explorer._deadline = time.monotonic() + 60
    screen = explorer._record(0, "screens", {
        "url": explorer_module.TARGET, "title": "Catalog", "text_excerpt": "Pear drink", "element_ids": []})
    action = explorer._record(0, "actions", {"kind": "open", "label": "시작 URL"})
    request = explorer._record(0, "requests", {"action_id": action["id"], "source": "browser",
        "method": "GET", "url": explorer_module.TARGET + "catalog?query=", "query": {"query": {"value": "", "type": "string"}}})
    response = explorer._record(0, "responses", {"request_id": request["id"], "status": 200,
        "content_type": "application/json", **explorer_module.summarize_body(b'{"items":[{"name":"Pear drink"}]}', "application/json")})
    return explorer, screen, request, response


class PlanningRegressionTests(unittest.TestCase):
    def test_executor_allows_observed_read_navigation_and_rejects_write_controls(self):
        element = {"kind": "a", "href": explorer_module.TARGET + "#/catalog-new", "label": "Browse", "visible": True}
        self.assertEqual(explorer_module.V2Explorer._element_actions(element), ["click"])
        for changes in ({"href": "https://example.com/"}, {"href": explorer_module.TARGET + "#/checkout"},
                        {"label": "Delete item"}, {"form_method": "post"}, {"disabled": True}):
            with self.subTest(changes=changes):
                self.assertEqual(explorer_module.V2Explorer._element_actions(dict(element, **changes)), [])

    def test_stale_elements_and_exhausted_budgets_cannot_be_executed(self):
        decision = {"plan": ["화면 확인"], "reason": "관찰", "action": "click", "element_id": "EL-0001", "request_id": "", "value": ""}
        elements = [{"id": "EL-0001", "allowed_actions": ["click"]}]
        self.assertIsNone(validate_decision(decision, elements, [], 1, 1))
        self.assertIsNotNone(validate_decision(decision, [], [], 1, 1))
        self.assertIsNotNone(validate_decision(decision, elements, [], 0, 1))
        decision.update(action="experiment", request_id="REQ-0001")
        self.assertIsNotNone(validate_decision(decision, elements, [{"id": "REQ-0001"}], 1, 0))

    def test_observed_read_requests_are_available_without_scripted_search(self):
        explorer, _, request, _ = observed_run()
        self.assertEqual([r["id"] for r in explorer._eligible()], [request["id"]])

    def test_search_rejects_tried_value_but_allows_another_model_value(self):
        elements = [{"id": "EL-0001", "allowed_actions": ["search"], "blocked_values": ["Pear"]}]
        decision = {"plan": ["조회 결과 비교"], "reason": "새 검색값을 확인", "action": "search",
                    "element_id": "EL-0001", "request_id": "", "value": "Pear"}
        self.assertIsNotNone(validate_decision(decision, elements, [], 2, 1))
        decision["value"] = "Cocoa"
        self.assertIsNone(validate_decision(decision, elements, [], 2, 1))

    def test_hypothesis_receives_action_and_response_evidence(self):
        explorer, _, request, response = observed_run()
        stream = io.BytesIO(b'{"message":{"content":"{}"}}\n')
        with mock_model(return_value=stream) as opened:
            asyncio.run(explorer._ask_ollama(0, request, response))
        prompt = json.loads(json.loads(opened.call_args.args[0].content)["messages"][1]["content"])
        self.assertEqual(prompt["observed_response"]["sample"]["items"][0]["name"], "Pear drink")
        self.assertEqual(prompt["observed_action"]["kind"], "open")

    def test_planner_receives_current_screen_and_previous_experiment(self):
        explorer, screen, request, _ = observed_run()
        explorer.state["experiments"].append({"id": "EXP-001", "result": "변화 관찰",
            "differences": {"baseline_vs_changed": {"changed_fields": [{"field": "list_length", "baseline": 46, "changed": 1}]}}})
        stream = io.BytesIO(b'{"message":{"content":"{}"}}\n')
        with mock_model(return_value=stream) as opened:
            asyncio.run(explorer._ask_plan(0, screen))
        prompt = json.loads(json.loads(opened.call_args.args[0].content)["messages"][1]["content"])
        self.assertEqual(prompt["screen"]["text_excerpt"], "Pear drink")
        self.assertEqual(prompt["requests"][0]["id"], request["id"])
        self.assertEqual(prompt["experiments"][0]["differences"]["baseline_vs_changed"]["changed_fields"][0]["changed"], 1)

    def test_model_schema_excludes_unavailable_tools_and_ids(self):
        explorer, screen, request, _ = observed_run()
        explorer.state["hypotheses"] = [{}] * explorer_module.MAX_HYPOTHESES
        stream = io.BytesIO(b'{"message":{"content":"{}"}}\n')
        with mock_model(return_value=stream) as opened:
            asyncio.run(explorer._ask_plan(0, screen))
        properties = json.loads(opened.call_args.args[0].content)["format"]["properties"]
        self.assertEqual(properties["action"]["enum"], ["finish"])
        self.assertEqual(properties["element_id"].get("enum"), [""])
        self.assertEqual(properties["request_id"].get("enum"), [""])

    def test_model_schema_pairs_actions_with_their_valid_element_ids(self):
        explorer, screen, _, _ = observed_run()
        button = explorer._record(0, "elements", {"screen_id": screen["id"], "kind": "button", "label": "Browse",
                                                  "allowed_actions": ["click"]})
        field = explorer._record(0, "elements", {"screen_id": screen["id"], "kind": "input", "label": "Search",
                                                 "allowed_actions": ["search"]})
        stream = io.BytesIO(b'{"message":{"content":"{}"}}\n')
        with mock_model(return_value=stream) as opened:
            asyncio.run(explorer._ask_plan(0, screen))
        branches = json.loads(opened.call_args.args[0].content)["format"].get("oneOf", [])
        self.assertTrue(branches, "Action and target must be constrained together")
        choices = {b["properties"]["action"]["const"]: b["properties"] for b in branches}
        self.assertEqual(choices["click"]["element_id"]["enum"], [button["id"]])
        self.assertEqual(choices["search"]["element_id"]["enum"], [field["id"]])
        self.assertEqual(choices["click"]["value"]["const"], "")
        self.assertEqual(choices["search"]["request_id"]["const"], "")

    def test_model_finish_does_not_trigger_scripted_search_or_experiment(self):
        explorer, screen, _, _ = observed_run()
        decision = {"plan": ["관찰된 카탈로그를 확인했다"], "reason": "추가 동작 없이 종료",
            "action": "finish", "element_id": "", "request_id": "", "value": ""}
        stream = io.BytesIO((json.dumps({"message": {"content": json.dumps(decision)}}) + "\n").encode())
        with mock_model(return_value=stream):
            asyncio.run(explorer._agent_loop(0, None, None, None, screen))
        self.assertEqual(explorer.state["plan"], ["관찰된 카탈로그를 확인했다"])
        self.assertEqual(explorer.state["finish_reason"], "추가 동작 없이 종료")
        self.assertEqual(len(explorer.state["observations"]["actions"]), 1)
        self.assertEqual(explorer.state["hypotheses"], [])

    def test_invalid_element_is_rejected_without_replacing_model_choice(self):
        explorer, screen, _, _ = observed_run()
        invalid = {"plan": ["없는 링크 열기"], "reason": "확인", "action": "click",
            "element_id": "EL-9999", "request_id": "", "value": ""}
        finish = dict(invalid, action="finish", element_id="", reason="잘못된 선택을 취소")
        streams = [io.BytesIO((json.dumps({"message": {"content": json.dumps(d)}}) + "\n").encode()) for d in (invalid, finish)]
        payloads = []
        def answer(request, **kwargs):
            payloads.append(json.loads(request.content))
            return streams.pop(0)
        with mock_model(side_effect=answer):
            asyncio.run(explorer._agent_loop(0, None, None, None, screen))
        feedback = json.loads(payloads[1]["messages"][1]["content"])["recent_decisions"][-1]
        self.assertEqual(feedback["status"], "rejected")
        self.assertTrue(feedback["error"])
        self.assertEqual(len(explorer.state["observations"]["actions"]), 1)

    def test_non_object_decision_is_rejected_and_llm_can_replan(self):
        explorer, screen, _, _ = observed_run()
        explorer._record(0, "elements", {"screen_id": screen["id"], "kind": "button", "label": "Details",
                                        "allowed_actions": ["click"]})
        finish = {"plan": ["관찰 종료"], "reason": "형식을 수정하여 종료", "action": "finish",
                  "element_id": "", "request_id": "", "value": ""}
        streams = [io.BytesIO((json.dumps({"message": {"content": json.dumps(d)}}) + "\n").encode())
                   for d in ([], finish)]
        with mock_model(side_effect=lambda *args: streams.pop(0)):
            asyncio.run(explorer._agent_loop(0, None, None, None, screen))
        self.assertEqual([d["status"] for d in explorer.state["decisions"]], ["rejected", "completed"])
        self.assertEqual(explorer.state["finish_reason"], "형식을 수정하여 종료")

    def test_unavailable_planner_reports_failure_without_fallback(self):
        explorer, screen, _, _ = observed_run()
        with mock_model(side_effect=httpx.ConnectError("offline")):
            with self.assertRaisesRegex(RuntimeError, "계획 생성 실패"):
                asyncio.run(explorer._agent_loop(0, None, None, None, screen))
        self.assertEqual(explorer.state["hypotheses"], [])
        self.assertEqual(len(explorer.state["observations"]["actions"]), 1)


if __name__ == "__main__":
    unittest.main()

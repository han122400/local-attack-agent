import io
import json
import time
import unittest
import asyncio
from unittest.mock import patch

import v2
from planning import validate_decision


def observed_run():
    explorer = v2.V2Explorer()
    explorer._deadline = time.monotonic() + 60
    screen = explorer._record(0, "screens", {
        "url": v2.TARGET, "title": "Catalog", "text_excerpt": "Pear drink", "element_ids": []})
    action = explorer._record(0, "actions", {"kind": "open", "label": "시작 URL"})
    request = explorer._record(0, "requests", {"action_id": action["id"], "source": "browser",
        "method": "GET", "url": v2.TARGET + "catalog?query=", "query": {"query": {"value": "", "type": "string"}}})
    response = explorer._record(0, "responses", {"request_id": request["id"], "status": 200,
        "content_type": "application/json", **v2.summarize_body(b'{"items":[{"name":"Pear drink"}]}', "application/json")})
    return explorer, screen, request, response


class PlanningRegressionTests(unittest.TestCase):
    def test_executor_allows_observed_read_navigation_and_rejects_write_controls(self):
        element = {"kind": "a", "href": v2.TARGET + "#/catalog-new", "label": "Browse", "visible": True}
        self.assertEqual(v2.V2Explorer._element_actions(element), ["click"])
        for changes in ({"href": "https://example.com/"}, {"href": v2.TARGET + "#/checkout"},
                        {"label": "Delete item"}, {"form_method": "post"}, {"disabled": True}):
            with self.subTest(changes=changes):
                self.assertEqual(v2.V2Explorer._element_actions(dict(element, **changes)), [])

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

    def test_hypothesis_receives_action_and_response_evidence(self):
        explorer, _, request, response = observed_run()
        stream = io.BytesIO(b'{"message":{"content":"{}"}}\n')
        with patch("v2.urlopen", return_value=stream) as opened:
            explorer._ask_ollama(0, request, response)
        prompt = json.loads(json.loads(opened.call_args.args[0].data)["messages"][1]["content"])
        self.assertEqual(prompt["observed_response"]["sample"]["items"][0]["name"], "Pear drink")
        self.assertEqual(prompt["observed_action"]["kind"], "open")

    def test_planner_receives_current_screen_and_previous_experiment(self):
        explorer, screen, request, _ = observed_run()
        explorer.state["experiments"].append({"id": "EXP-001", "result": "변화 관찰",
            "differences": {"baseline_vs_changed": {"changed_fields": [{"field": "list_length", "baseline": 46, "changed": 1}]}}})
        stream = io.BytesIO(b'{"message":{"content":"{}"}}\n')
        with patch("v2.urlopen", return_value=stream) as opened:
            explorer._ask_plan(0, screen)
        prompt = json.loads(json.loads(opened.call_args.args[0].data)["messages"][1]["content"])
        self.assertEqual(prompt["screen"]["text_excerpt"], "Pear drink")
        self.assertEqual(prompt["requests"][0]["id"], request["id"])
        self.assertEqual(prompt["experiments"][0]["differences"]["baseline_vs_changed"]["changed_fields"][0]["changed"], 1)

    def test_model_finish_does_not_trigger_scripted_search_or_experiment(self):
        explorer, screen, _, _ = observed_run()
        decision = {"plan": ["관찰된 카탈로그를 확인했다"], "reason": "추가 동작 없이 종료",
            "action": "finish", "element_id": "", "request_id": "", "value": ""}
        stream = io.BytesIO((json.dumps({"message": {"content": json.dumps(decision)}}) + "\n").encode())
        with patch("v2.urlopen", return_value=stream):
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
            payloads.append(json.loads(request.data))
            return streams.pop(0)
        with patch("v2.urlopen", side_effect=answer):
            asyncio.run(explorer._agent_loop(0, None, None, None, screen))
        feedback = json.loads(payloads[1]["messages"][1]["content"])["recent_decisions"][-1]
        self.assertEqual(feedback["status"], "rejected")
        self.assertTrue(feedback["error"])
        self.assertEqual(len(explorer.state["observations"]["actions"]), 1)

    def test_unavailable_planner_reports_failure_without_fallback(self):
        explorer, screen, _, _ = observed_run()
        with patch("v2.urlopen", side_effect=v2.URLError("offline")):
            with self.assertRaisesRegex(RuntimeError, "계획 생성 실패"):
                asyncio.run(explorer._agent_loop(0, None, None, None, screen))
        self.assertEqual(explorer.state["hypotheses"], [])
        self.assertEqual(len(explorer.state["observations"]["actions"]), 1)


if __name__ == "__main__":
    unittest.main()

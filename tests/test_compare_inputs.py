"""Run the worker's real helper functions against a page adapter; never launch a browser."""

import ast
import unittest
from types import SimpleNamespace

from code_checks import check_generated_code
from command_runtime import WORKER_SOURCE


class FixturePage:
    def __init__(self, *, unstable=False, fail_test=False, changed_field=False, empty=False):
        self.url = "http://127.0.0.1:3000/search"
        self.value = ""
        self.values = []
        self.unstable, self.fail_test, self.changed_field = unstable, fail_test, changed_field
        self.empty = empty
        self.keyboard = SimpleNamespace(press=self.press)

    async def press(self, key):
        self.assert_enter = key == "Enter"
        self.values.append(self.value)
        if self.fail_test and self.value == "test":
            raise RuntimeError("fixture submit failed")

    async def wait_for_timeout(self, milliseconds):
        pass

    async def title(self):
        return "Fixture search"

    async def evaluate(self, script, argument=None):
        if argument is None:
            return {"inputs": [{"index": 0, "tag": "input", "type": "text", "name": "other" if self.changed_field and self.values else "q",
                                "id": "search", "label": "Search"}], "buttons": ["Search"]}
        if isinstance(argument, list):
            self.value = argument[1]
            return True
        if argument != ".result":
            raise AssertionError("The comparison must use the same result selector")
        count = 0 if self.empty else 5 if self.value == "test" else 2 if self.unstable and self.values.count("normal") > 1 else 1
        return {"count": count, "sample": [f"item-{i}" for i in range(min(count, 5))]}


def worker_helpers(page):
    tree = ast.parse(WORKER_SOURCE)
    names = {"get_text", "get_json", "http_get", "goto_and_render", "list_inputs", "submit_input", "compare_inputs"}
    nodes = [node for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef) and node.name in names]
    nodes += [node for node in ast.walk(tree) if isinstance(node, ast.Assign)
              and any(isinstance(t, ast.Name) and t.id == "helpers" for t in node.targets)]
    namespace = {"page": page, "target_origin": "http://127.0.0.1:3000"}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "worker_helpers", "exec"), namespace)
    return namespace["helpers"]


class CompareInputsTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_helper_collects_normal_test_and_recheck(self):
        page = FixturePage()
        helpers = worker_helpers(page)
        self.assertIn("compare_inputs", helpers)
        result = await helpers["compare_inputs"](0, "normal", "test", ".result", settle_ms=0)
        self.assertEqual(page.values, ["normal", "test", "normal"])
        self.assertEqual(result["status"], "compared")
        self.assertEqual(result["comparison"]["result_count_delta"], 4)
        self.assertTrue(result["comparison"]["sample_changed"])
        self.assertTrue(result["comparison"]["baseline_sample_stable"])
        self.assertEqual(result["samples"]["normal"]["result_count"], 1)
        self.assertNotIn("confirmed", result)

    async def test_changed_baseline_is_inconclusive(self):
        helpers = worker_helpers(FixturePage(unstable=True))
        self.assertIn("compare_inputs", helpers)
        result = await helpers["compare_inputs"](0, "normal", "test", ".result", settle_ms=0)
        self.assertEqual(result["status"], "inconclusive")
        self.assertFalse(result["comparison"]["baseline_sample_stable"])

    async def test_changed_input_identity_stops_before_test(self):
        page = FixturePage(changed_field=True)
        helpers = worker_helpers(page)
        self.assertIn("compare_inputs", helpers)
        result = await helpers["compare_inputs"](0, "normal", "test", ".result", settle_ms=0)
        self.assertEqual(result["status"], "inconclusive")
        self.assertEqual(page.values, ["normal"])

    async def test_failure_retains_partial_observation(self):
        page = FixturePage(fail_test=True)
        helpers = worker_helpers(page)
        self.assertIn("compare_inputs", helpers)
        result = await helpers["compare_inputs"](0, "normal", "test", ".result", settle_ms=0)
        self.assertEqual(result["status"], "inconclusive")
        self.assertIn("normal", result["samples"])
        self.assertEqual(page.values, ["normal", "test"])

    async def test_selector_is_required_before_submission(self):
        page = FixturePage()
        helpers = worker_helpers(page)
        self.assertIn("compare_inputs", helpers)
        with self.assertRaises(ValueError):
            await helpers["compare_inputs"](0, "normal", "test", "")
        self.assertEqual(page.values, [])

    async def test_selector_matching_nothing_is_inconclusive(self):
        result = await worker_helpers(FixturePage(empty=True))["compare_inputs"](0, "normal", "test", ".result", settle_ms=0)
        self.assertEqual(result["status"], "inconclusive")

    async def test_identical_values_are_rejected_before_submission(self):
        page = FixturePage()
        with self.assertRaises(ValueError):
            await worker_helpers(page)["compare_inputs"](0, "same", "same", ".result")
        self.assertEqual(page.values, [])

    async def test_batched_samples_match_three_manual_submissions(self):
        manual = worker_helpers(FixturePage())
        expected = {}
        for stage, value in (("normal", "normal"), ("test", "test"), ("recheck", "normal")):
            expected[stage] = await manual["submit_input"](0, value, ".result", settle_ms=0)
        batched = await worker_helpers(FixturePage())["compare_inputs"](0, "normal", "test", ".result", settle_ms=0)
        self.assertEqual(batched["samples"], expected)

    def test_static_check_requires_await_for_comparison(self):
        self.assertTrue(check_generated_code("compare_inputs(0, 'normal', 'test', '.result')"))
        self.assertEqual(check_generated_code("result = await compare_inputs(0, 'normal', 'test', '.result')"), [])


if __name__ == "__main__":
    unittest.main()

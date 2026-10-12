"""Regression tests for the v3 Command contract."""

import unittest

from planning import COMMAND_SCHEMA, SYSTEM_PROMPT, validate_command


def answer(action="finish", **fields):
    result = {"action": action, "plan": "Observe the app", "reason": "Need evidence",
              "path": "", "content": "", "command": "", "code": ""}
    result.update(fields)
    return result


class PlanningRegressionTests(unittest.TestCase):
    def test_schema_offers_general_commands_without_old_element_ids(self):
        self.assertEqual(set(COMMAND_SCHEMA["properties"]["action"]["enum"]),
                         {"read_file", "write_file", "run_command", "run_python", "finish"})
        for old in ("element_id", "request_id", "allowed_actions", "search", "click", "experiment"):
            self.assertNotIn(old, COMMAND_SCHEMA["properties"])
        self.assertIn("browser", SYSTEM_PROMPT)

    def test_python_code_can_choose_search_and_navigation_itself(self):
        code = "await page.goto(target)\nawait page.locator('input').fill('a phrase longer than thirty characters chosen by the model')"
        command = validate_command(answer("run_python", code=code))
        self.assertEqual(command.code, code)

    def test_action_inputs_cannot_be_mixed(self):
        self.assertEqual(validate_command(answer("run_command", command="dir")).action, "run_command")
        with self.assertRaises(ValueError):
            validate_command(answer("run_command", command="dir", code="print(1)"))
        with self.assertRaises(ValueError):
            validate_command(answer("click", path="EL-1"))
        with self.assertRaises(ValueError):
            validate_command(answer("finish", element_id="EL-1"))


if __name__ == "__main__":
    unittest.main()

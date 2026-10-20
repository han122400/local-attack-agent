import unittest

from code_checks import check_generated_code


class CodeCheckTests(unittest.TestCase):
    def test_clean_code_passes(self):
        self.assertEqual(check_generated_code(
            "await page.goto('http://127.0.0.1:3000/')\nprint(await page.title())"), [])
        self.assertEqual(check_generated_code(
            "async def main(page, context, browser):\n    await page.goto('x')\n    return await page.title()"), [])
        self.assertEqual(check_generated_code(
            "inputs = await list_inputs()\nr = await submit_input(inputs['inputs'][0]['index'], 'apple')"), [])

    def test_catches_observed_failures(self):
        self.assertIn("asyncio.run", " ".join(check_generated_code("asyncio.run(main())")))
        self.assertTrue(any("import" in p for p in check_generated_code("from playwright.async_api import Page")))
        self.assertTrue(any("import" in p for p in check_generated_code("import playwright")))
        self.assertTrue(any("main" in p for p in check_generated_code("async def main(page):\n    pass")))
        self.assertTrue(any("async def" in p for p in check_generated_code("def main(page, context, browser):\n    pass")))
        self.assertTrue(any("await" in p for p in check_generated_code("page.goto('http://x')")))
        self.assertTrue(any("await" in p for p in check_generated_code("x = list_inputs()")))

    def test_syntax_error_is_reported_not_raised(self):
        problems = check_generated_code("print(f'bad {x[0]\\n}')")
        self.assertTrue(problems and "Syntax error" in problems[0])

    def test_no_false_positives(self):
        # str.title() and builtins must not be flagged as missing-await Playwright calls
        self.assertEqual(check_generated_code("print('abc'.title())"), [])
        self.assertEqual(check_generated_code("y = type(5)\nprint(y)"), [])
        self.assertEqual(check_generated_code("items = [1, 2]\nfirst = items[0]"), [])


if __name__ == "__main__":
    unittest.main()

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
        self.assertTrue(check_generated_code("return 1"))

    def test_no_false_positives(self):
        # str.title() and builtins must not be flagged as missing-await Playwright calls
        self.assertEqual(check_generated_code("print('abc'.title())"), [])
        self.assertEqual(check_generated_code("y = type(5)\nprint(y)"), [])
        self.assertEqual(check_generated_code("items = [1, 2]\nfirst = items[0]"), [])

    def test_undefined_main_is_caught_before_prior_side_effects_execute(self):
        problems = check_generated_code("print('request already sent')\nawait main(page, context, browser)")
        self.assertTrue(any("main" in p and "defined" in p for p in problems))
        for source in ("async def main(page, context, browser):\n    pass\nawait main(page, context, browser)",
                       "from hooks import main\nawait main(page, context, browser)",
                       "main = handler\nawait main(page, context, browser)"):
            self.assertEqual(check_generated_code(source), [])

    def test_observed_request_api_mistakes_get_precise_feedback(self):
        self.assertTrue(any("context manager" in p for p in check_generated_code(
            "async with context.request as request:\n    pass")))
        self.assertTrue(any("text()" in p for p in check_generated_code(
            "r = await context.request.get('https://fixture.invalid/')\nprint(r.text[:100])")))
        self.assertTrue(any("Page" in p for p in check_generated_code(
            "r = await goto_and_render('https://fixture.invalid/')\nprint(r.get('status'))")))
        self.assertEqual(check_generated_code(
            "r = await context.request.get('https://fixture.invalid/')\nprint(await r.text())"), [])
        self.assertEqual(check_generated_code("r = {'text':'value'}\nprint(r.get('text'))"), [])

    def test_response_checks_allow_method_references_gather_and_rebound_variables(self):
        for source in ("r = await context.request.get('x')\ncallback = r.text",
                       "r = await context.request.get('x')\nawait asyncio.gather(r.text())",
                       "r = await context.request.get('x')\nfor r in rows:\n    print(r.text[:10])"):
            with self.subTest(source=source):
                self.assertEqual(check_generated_code(source), [])

    def test_awaiting_synchronous_helpers_is_caught_before_prior_side_effects(self):
        for source in ("await page.goto('x')\nprint(await captured_requests())",
                       "print(await discover_inputs('text', 'https://example.test'))",
                       "await note_candidate('download')"):
            with self.subTest(source=source):
                self.assertTrue(any("synchronous" in problem for problem in check_generated_code(source)))

    def test_sync_helper_guard_allows_local_async_overrides_and_thread_offloading(self):
        for source in ("async def captured_requests():\n    return []\nprint(await captured_requests())",
                       "from custom import captured_requests\nprint(await captured_requests())",
                       "captured_requests = custom\nprint(await captured_requests())",
                       "await asyncio.to_thread(captured_requests)",
                       "print(captured_requests())"):
            with self.subTest(source=source):
                self.assertEqual(check_generated_code(source), [])

    def test_observed_fictitious_page_method_and_helper_module_get_specific_feedback(self):
        self.assertTrue(any("page.content" in p for p in check_generated_code("print(await page.get_text())")))
        self.assertTrue(any("provided" in p for p in check_generated_code("import http_get\nawait http_get('x')")))

    def test_http_helper_dictionary_is_not_a_native_response_object(self):
        source = "response = await http_get('x')\nprint(response.status, response.body)"
        self.assertTrue(any("dictionary" in p for p in check_generated_code(source)))
        self.assertEqual(check_generated_code("r = await http_get('x')\nprint(r['status'], r['body'])"), [])
        self.assertEqual(check_generated_code("async def http_get(url):\n    return custom\nr = await http_get('x')\nprint(r.status)"), [])

    def test_async_batching_and_scheduling_are_not_rejected_as_missing_await(self):
        for source in ("await asyncio.gather(http_get('x'), page.title())",
                       "await asyncio.gather(*(get_text(url) for url in urls))",
                       "await asyncio.wait_for(page.goto('x'), timeout=5)",
                       "task = asyncio.create_task(get_json('x'))\nprint(await task)"):
            with self.subTest(source=source):
                self.assertEqual(check_generated_code(source), [])

    def test_runtime_imports_keep_known_types_and_custom_functions_remain_allowed(self):
        self.assertTrue(any("synchronous" in p for p in check_generated_code(
            "from tools import captured_requests\nawait captured_requests()")))
        self.assertTrue(any("dictionary" in p for p in check_generated_code(
            "from tools import http_get\nr = await http_get('x')\nprint(r.status)")))
        self.assertEqual(check_generated_code("def get_text(url):\n    return 'local'\nprint(get_text('x'))"), [])


if __name__ == "__main__":
    unittest.main()

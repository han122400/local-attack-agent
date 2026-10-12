"""Search affordances from real browser DOM; no Ollama or Juice Shop connection."""

import unittest

import explorer as explorer_module


class InputRecognitionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.explorer = explorer_module.V2Explorer()
        self.playwright = await explorer_module.async_playwright().start()
        executable = explorer_module.installed_chromium()
        self.browser = await self.playwright.chromium.launch(
            headless=True,
            **({"executable_path": executable} if executable else {}),
        )
        self.context = await self.browser.new_context(service_workers="block")
        await self.context.route("**/*", lambda route: route.fulfill(
            content_type="text/html", body="<!doctype html><title>Input fixture</title>"
        ))
        self.page = await self.context.new_page()
        await self.page.goto(explorer_module.TARGET)

    async def asyncTearDown(self):
        await self.context.close()
        await self.browser.close()
        await self.playwright.stop()

    async def offered_elements(self, html):
        await self.page.set_content("<!doctype html><title>Input fixture</title>" + html)
        screen = await self.explorer._screen(0, self.page, None)
        return self.explorer._plan_context(screen)["elements"]

    async def test_associated_label_makes_plain_text_input_searchable(self):
        elements = await self.offered_elements("""
            <label for="catalog-query">Search products</label>
            <input id="catalog-query" type="text">
        """)
        self.assertEqual(len(elements), 1)
        self.assertIn("search", elements[0]["allowed_actions"])

    async def test_aria_labelledby_makes_plain_text_input_searchable(self):
        elements = await self.offered_elements("""
            <span id="query-label">상품 검색</span>
            <input type="text" aria-labelledby="query-label">
        """)
        self.assertEqual(len(elements), 1)
        self.assertIn("search", elements[0]["allowed_actions"])

    async def test_search_region_makes_unlabelled_text_input_searchable(self):
        elements = await self.offered_elements("""
            <section role="search"><input type="text"></section>
        """)
        self.assertEqual(len(elements), 1)
        self.assertIn("search", elements[0]["allowed_actions"])

    async def test_ancestor_search_label_makes_open_text_input_searchable(self):
        elements = await self.offered_elements("""
            <div aria-label="Click to search"><div>
              <span>search</span>
              <input type="text" placeholder="" tabindex="-1"
                     style="width:220px; padding:2px; border:0">
              <span>close</span>
            </div></div>
        """)
        self.assertEqual(len(elements), 1)
        self.assertIn("search", elements[0]["allowed_actions"])

    async def test_padding_only_closed_search_input_is_not_offered(self):
        elements = await self.offered_elements("""
            <div aria-label="Click to search"><div>
              <span>search</span>
              <input type="text" placeholder="" tabindex="-1"
                     style="width:0; min-width:0; padding:2px; border:0">
              <span>close</span>
            </div></div>
        """)
        self.assertEqual(elements, [])

    async def test_display_none_input_is_not_offered(self):
        elements = await self.offered_elements("""
            <input type="search" aria-label="Hidden search" style="display:none">
            <input type="search" aria-label="Visible search">
        """)
        self.assertEqual([element["label"] for element in elements], ["Visible search"])

    async def test_visibility_hidden_input_is_not_offered(self):
        elements = await self.offered_elements("""
            <input type="search" aria-label="Hidden search" style="visibility:hidden">
            <input type="search" aria-label="Visible search">
        """)
        self.assertEqual([element["label"] for element in elements], ["Visible search"])

    async def test_zero_width_input_with_client_rect_is_not_offered(self):
        elements = await self.offered_elements("""
            <input type="search" aria-label="Collapsed search"
                   style="width:0; min-width:0; padding:0; border:0">
            <input type="search" aria-label="Visible search">
        """)
        # Chromium still gives the collapsed input a client rect and a height;
        # neither makes it an input that the agent can interact with.
        self.assertGreater(await self.page.locator('[aria-label="Collapsed search"]').evaluate(
            "el => el.getClientRects().length"
        ), 0)
        self.assertEqual([element["label"] for element in elements], ["Visible search"])

    async def test_password_input_in_search_region_is_not_offered(self):
        elements = await self.offered_elements("""
            <section role="search">
              <label for="secret-query">Search products</label>
              <input id="secret-query" type="password">
            </section>
        """)
        self.assertEqual(elements, [])

    async def test_post_form_search_input_is_not_offered(self):
        elements = await self.offered_elements("""
            <form method="post" action="/catalog">
              <label for="post-query">Search products</label>
              <input id="post-query" type="text" aria-label="Search products">
            </form>
        """)
        self.assertEqual(elements, [])


if __name__ == "__main__":
    unittest.main()

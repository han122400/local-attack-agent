"""Real browser regressions for semantic input discovery and bounded replanning."""

import io
import json
import time
import unittest
from urllib.parse import parse_qs, urlsplit

import explorer as explorer_module
from model_transport import mock_model


HTML = """<!doctype html><title>Search and repeat fixture</title>
<section role="search" aria-label="Catalog search">
  <button id="open-search" type="button" onclick="toggleSearch(true)">Open search</button>
  <div id="search-panel" hidden>
    <button id="close-search" type="button" onclick="toggleSearch(false)">Close search</button>
    <input id="catalog-search" type="text" autocomplete="off">
    <output id="result"></output>
  </div>
</section>
<button id="noop" type="button" onclick="window.interactions.push('noop')">Inspect unchanged panel</button>
<button id="first-details" type="button" onclick="inspectDetails('first')">Details</button>
<button id="second-details" type="button" onclick="inspectDetails('second')">Details</button>
<span id="first-result"></span><span id="second-result"></span>
<script>
window.interactions = [];
window.searches = [];
function toggleSearch(open) {
  document.querySelector('#search-panel').hidden = !open;
  document.querySelector('#open-search').hidden = open;
  window.interactions.push(open ? 'open' : 'close');
}
function inspectDetails(which) {
  document.querySelector('#' + which + '-result').textContent = which + ' inspected';
  window.interactions.push(which);
}
async function load(value) {
  const response = await fetch('/catalog?query=' + encodeURIComponent(value));
  const body = await response.json();
  document.querySelector('#result').textContent = JSON.stringify(body);
}
document.querySelector('input').addEventListener('keydown', event => {
  if (event.key === 'Enter') {
    window.searches.push(event.target.value);
    load(event.target.value);
  }
});
load('');
</script>"""


def decision(action="finish", element_id="", value=""):
    return {"plan": ["관찰된 기능을 선택해서 확인한다"], "reason": "현재 관찰 근거를 확인한다",
            "action": action, "element_id": element_id, "request_id": "", "value": value}


class RepeatActionBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.explorer = explorer_module.V2Explorer()
        self.explorer._deadline = time.monotonic() + 90
        self.transcripts = []
        self.playwright = await explorer_module.async_playwright().start()
        executable = explorer_module.installed_chromium()
        self.browser = await self.playwright.chromium.launch(
            headless=True, **({"executable_path": executable} if executable else {}))
        self.context = await self.browser.new_context(service_workers="block")
        self.fixture_html = HTML

        async def serve(route):
            if not explorer_module.allowed_url(route.request.url):
                await route.abort()
            elif urlsplit(route.request.url).path == "/catalog":
                value = parse_qs(urlsplit(route.request.url).query).get("query", [""])[0]
                items = [{"name": "Pear"}, {"name": "Cocoa"}]
                await route.fulfill(json={"items": [item for item in items if not value or item["name"] == value]})
            else:
                await route.fulfill(content_type="text/html", body=self.fixture_html)

        await self.context.route("**/*", serve)
        self.page = await self.context.new_page()
        self.tracker = explorer_module._Tracker(self.explorer, 0, self.page)
        self.page.on("request", self.tracker.on_request)
        self.page.on("response", self.tracker.on_response)

    async def asyncTearDown(self):
        await self.context.close()
        await self.browser.close()
        await self.playwright.stop()

    async def run_choices(self, choose):
        screen = await self.explorer._recon(0, self.page, self.tracker)

        def respond(request):
            payload = json.loads(request.content)
            prompt = json.loads(payload["messages"][1]["content"])
            self.transcripts.append(prompt)
            answer = choose(prompt, len(self.transcripts))
            return io.BytesIO((json.dumps({"message": {"content": json.dumps(answer, ensure_ascii=False)}}) + "\n").encode())

        with mock_model(side_effect=respond):
            await self.explorer._agent_loop(0, self.page, self.tracker, self.context, screen)

    @staticmethod
    def offered(prompt, label):
        return next((element for element in prompt["elements"] if element["label"] == label), None)

    def current_observed(self, label):
        screen_id = self.explorer.state["observations"]["screens"][-1]["id"]
        return next(element for element in self.explorer.state["observations"]["elements"]
                    if element["screen_id"] == screen_id and element["label"] == label)

    async def test_reopening_allows_model_chosen_search_in_unlabeled_text_input(self):
        # Regresses URL-only repeat suppression and missing search-component semantics.
        def choose(prompt, number):
            if number <= 3:
                label = "Close search" if number == 2 else "Open search"
                element = self.offered(prompt, label)
                return decision("click", element["id"]) if element else decision()
            if number == 4:
                element = next((element for element in prompt["elements"]
                                if "search" in element["allowed_actions"]), None)
                return decision("search", element["id"], "Pear") if element else decision()
            return decision()

        await self.run_choices(choose)
        self.assertEqual(await self.page.evaluate("window.interactions"), ["open", "close", "open"])
        self.assertEqual(await self.page.evaluate("window.searches"), ["Pear"])
        self.assertEqual(await self.page.locator("#catalog-search").input_value(), "Pear")
        self.assertTrue(all(item["status"] == "completed" for item in self.explorer.state["decisions"]))

    async def test_noop_control_is_removed_with_explicit_feedback(self):
        # Regresses the planner repeatedly receiving a known no-op as executable.
        def choose(prompt, number):
            if number == 1:
                return decision("click", self.offered(prompt, "Inspect unchanged panel")["id"])
            return decision()

        await self.run_choices(choose)
        offered_after = self.transcripts[1]
        self.assertIsNone(self.offered(offered_after, "Inspect unchanged panel"))
        blocked = offered_after.get("blocked_actions", [])
        self.assertTrue(blocked, "Planner needs explicit evidence explaining suppressed actions")
        self.assertIn("Inspect unchanged panel", json.dumps(blocked, ensure_ascii=False))
        self.assertEqual(await self.page.evaluate("window.interactions"), ["noop"])

    async def test_transition_cycle_is_capped_and_rejections_stall_without_crash(self):
        # Regresses either unbounded oscillation or RuntimeError after rejected choices.
        def choose(prompt, number):
            label = "Close search" if number in {2, 4} else "Open search"
            element = self.offered(prompt, label) or self.current_observed(label)
            return decision("click", element["id"])

        raised = None
        try:
            await self.run_choices(choose)
        except RuntimeError as error:
            raised = error
        self.assertIsNone(raised, f"A planner stall must be recorded without crashing: {raised}")
        self.assertEqual(await self.page.evaluate("window.interactions"), ["open", "close", "open", "close"])
        self.assertEqual(self.explorer.state["status"], "stalled")
        self.assertTrue(self.explorer.state["finish_reason"])
        self.assertEqual(self.explorer.state["errors"], [])
        self.assertEqual([item["status"] for item in self.explorer.state["decisions"][-3:]],
                         ["rejected", "rejected", "rejected"])
        self.assertIsNone(self.offered(self.transcripts[4], "Open search"))
        self.assertTrue(self.transcripts[4].get("blocked_actions"))

    async def test_identical_labels_do_not_conflate_distinct_controls(self):
        # Regresses element identity derived solely from kind/label/text.
        def choose(prompt, number):
            if number <= 2:
                elements = [element for element in prompt["elements"] if element["label"] == "Details"]
                return decision("click", elements[number - 1]["id"])
            return decision()

        await self.run_choices(choose)
        self.assertEqual(await self.page.evaluate("window.interactions"), ["first", "second"])
        details = [element for element in self.explorer.state["observations"]["elements"]
                   if element["screen_id"] == self.explorer.state["observations"]["screens"][0]["id"]
                   and element["label"] == "Details"]
        self.assertTrue(all(element.get("element_key") for element in details))
        self.assertEqual(len({element["element_key"] for element in details}), 2)

    async def test_search_repeat_blocks_value_but_allows_a_different_model_value(self):
        # Regresses removing the entire input instead of only its completed value.
        self.fixture_html = HTML.replace('<div id="search-panel" hidden>', '<div id="search-panel">').replace(
            'id="catalog-search" type="text"', 'id="catalog-search" type="search" aria-label="Catalog search"')

        def choose(prompt, number):
            if number <= 3:
                element = next(element for element in prompt["elements"] if "search" in element["allowed_actions"])
                return decision("search", element["id"], "Cocoa" if number == 3 else "Pear")
            return decision()

        await self.run_choices(choose)
        self.assertEqual(await self.page.evaluate("window.searches"), ["Pear", "Cocoa"])
        search = next(element for element in self.transcripts[1]["elements"] if "search" in element["allowed_actions"])
        self.assertIn("Pear", search.get("blocked_values", []))
        self.assertNotIn("Cocoa", search.get("blocked_values", []))
        self.assertEqual([item["status"] for item in self.explorer.state["decisions"]],
                         ["completed", "rejected", "completed", "completed"])

    async def test_transient_timer_and_status_changes_do_not_revive_a_noop(self):
        # Regresses clocks/status announcements bypassing repetition guards through body text.
        self.fixture_html = HTML.replace(
            "window.interactions.push('noop')",
            "window.interactions.push('noop'); document.querySelector('[role=timer]').textContent='2'; "
            "document.querySelector('[role=status]').textContent='Updated'").replace(
            '<span id="first-result">',
            '<span role="timer">1</span><div role="status">Ready</div><span id="first-result">')

        def choose(prompt, number):
            if number == 1:
                return decision("click", self.offered(prompt, "Inspect unchanged panel")["id"])
            return decision()

        await self.run_choices(choose)
        self.assertIsNone(self.offered(self.transcripts[1], "Inspect unchanged panel"))
        before = self.explorer.state["observations"]["screens"][-1]
        await self.page.locator("#first-result").evaluate("element => element.textContent = 'New catalog result'")
        after = await self.explorer._screen(0, self.page, None)
        self.assertNotEqual(before["state_key"], after["state_key"],
                            "Ordinary rendered result changes must remain meaningful")

    async def test_stable_dom_id_survives_insertion_of_an_earlier_sibling(self):
        # Regresses positional DOM paths overriding a control's stable ID.
        first = await self.explorer._recon(0, self.page, self.tracker)
        original = next(element for element in self.explorer.state["observations"]["elements"]
                        if element["screen_id"] == first["id"] and element["label"] == "Inspect unchanged panel")
        await self.page.evaluate("""() => {
            const button = document.createElement('button');
            button.type = 'button'; button.textContent = 'Extra control';
            document.body.insertBefore(button, document.body.firstElementChild);
        }""")
        second = await self.explorer._screen(0, self.page, None)
        moved = next(element for element in self.explorer.state["observations"]["elements"]
                     if element["screen_id"] == second["id"] and element["label"] == "Inspect unchanged panel")
        self.assertNotEqual(original["dom_path"], moved["dom_path"])
        self.assertEqual(original["element_key"], moved["element_key"])


if __name__ == "__main__":
    unittest.main()

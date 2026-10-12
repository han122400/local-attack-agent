"""Real Chromium, fixture site, deterministic model transport; no Ollama or lab writes."""
import asyncio
import io
import json
import time
import unittest
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

import v2
from model_transport import mock_model


HTML = """<!doctype html><title>Fixture catalog</title>
<button type="button" onclick="document.querySelector('input').hidden=false">Reveal catalog search</button>
<input type="search" aria-label="Catalog search" hidden>
<div id="result"></div>
<script>
async function load(value) {
 const response = await fetch('/catalog?query=' + encodeURIComponent(value));
 const body = await response.json();
 document.querySelector('#result').textContent = JSON.stringify(body);
}
document.querySelector('input').addEventListener('keydown', event => {
 if (event.key === 'Enter') load(event.target.value);
});
load('');
</script>"""


class BrowserPlannerTests(unittest.IsolatedAsyncioTestCase):
    async def test_model_choices_drive_dom_input_experiment_and_feedback(self):
        explorer = v2.V2Explorer()
        explorer._deadline = time.monotonic() + 60
        transcripts = []
        plan_count = 0

        def answer(request, **kwargs):
            nonlocal plan_count
            payload = json.loads(request.content)
            prompt = json.loads(payload["messages"][1]["content"])
            transcripts.append(prompt)
            if "claim" in payload["format"]["properties"]:
                observed = prompt["observed_request"]
                result = {"claim": "다른 상품 검색은 목록을 바꾼다", "baseline": {"request_id": observed["id"]},
                    "change": {"query_key": "query", "value": "Cocoa"},
                    "evidence_ids": [observed["id"], prompt["observed_response"]["id"]],
                    "expected_observation": "결과가 46개일 것이다", "required_state": "same_browser_session"}
            else:
                plan_count += 1
                result = {"plan": ["검색 입력을 관찰한다", "상품 조회 결과를 비교한다"], "reason": "관찰 근거를 추가한다",
                    "action": "finish", "element_id": "", "request_id": "", "value": ""}
                if plan_count == 1:
                    result.update(action="click", element_id=next(e["id"] for e in prompt["elements"] if e["kind"] == "button"))
                elif plan_count == 2:
                    result.update(action="search", value="Pear", element_id=next(e["id"] for e in prompt["elements"] if "search" in e["allowed_actions"]))
                elif plan_count == 3:
                    result.update(action="experiment", request_id=next(r["id"] for r in prompt["requests"] if r["url"].endswith("query=Pear")))
                else:
                    result.update(plan=["두 검색 결과의 차이를 확인했다"], reason="실험 결과를 확인하여 종료")
            return io.BytesIO((json.dumps({"message": {"content": json.dumps(result, ensure_ascii=False)}}) + "\n").encode())

        async with v2.async_playwright() as playwright:
            executable = v2.installed_chromium()
            browser = await playwright.chromium.launch(headless=True, **({"executable_path": executable} if executable else {}))
            context = await browser.new_context(service_workers="block")
            try:
                async def serve(route):
                    if not v2.allowed_url(route.request.url):
                        await route.abort()
                    elif urlsplit(route.request.url).path == "/catalog":
                        value = parse_qs(urlsplit(route.request.url).query).get("query", [""])[0]
                        items = [{"name": "Pear"}, {"name": "Cocoa"}]
                        await route.fulfill(json={"items": [i for i in items if not value or i["name"] == value]})
                    else:
                        await route.fulfill(content_type="text/html", body=HTML)
                await context.route("**/*", serve)
                page = await context.new_page()
                tracker = v2._Tracker(explorer, 0, page)
                page.on("request", tracker.on_request)
                page.on("response", tracker.on_response)
                screen = await explorer._recon(0, page, tracker)
                # BrowserContext.request bypasses browser routes; provide the same fixture at
                # the HTTP transport boundary so the real experiment and comparison still run.
                class FixtureResponse:
                    status = 200
                    headers = {"content-type": "application/json"}
                    def __init__(self, url):
                        self.url = url
                    async def body(self):
                        value = parse_qs(urlsplit(self.url).query)["query"][0]
                        return json.dumps({"items": [{"name": value}]}).encode()
                class FixtureHTTP:
                    async def get(self, url, **kwargs):
                        return FixtureResponse(url)
                class ExperimentContext:
                    request = FixtureHTTP()
                with mock_model(side_effect=answer):
                    await explorer._agent_loop(0, page, tracker, ExperimentContext(), screen)
                self.assertEqual(await page.locator("input").input_value(), "Pear")
                self.assertEqual([a["kind"] for a in explorer.state["observations"]["actions"]], ["open", "click", "search"])
                self.assertTrue(all(a.get("decision_id") for a in explorer.state["observations"]["actions"][1:]))
                self.assertEqual(explorer.state["hypotheses"][0]["change"]["value"], "Cocoa")
                self.assertEqual(explorer.state["experiments"][0]["result"], "변화 관찰")
                self.assertEqual(explorer.state["experiments"][0]["hypothesis_verdict"], "미판정")
                self.assertEqual(transcripts[-1]["experiments"][0]["result"], "변화 관찰")
                self.assertEqual(explorer.state["finish_reason"], "실험 결과를 확인하여 종료")
            finally:
                await context.close()
                await browser.close()


class WebSocketGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_production_browser_blocks_websockets_before_server_contact(self):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        received = threading.Event()
        class Probe(BaseHTTPRequestHandler):
            def do_GET(self):
                received.set()
                self.send_response(400)
                self.end_headers()
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Probe)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        explorer = v2.V2Explorer()
        async def recon(gen, page, tracker):
            await page.context.route(v2.TARGET, lambda route: route.fulfill(
                content_type="text/html", body="<!doctype html><title>Socket guard fixture</title>"))
            await page.goto(v2.TARGET)
            result = await page.evaluate("""async urls => Promise.all(urls.map(url => new Promise(resolve => {
                const socket = new WebSocket(url);
                socket.onclose = event => resolve(event.code);
            })))""", [f"ws://127.0.0.1:{server.server_port}/probe", "ws://127.0.0.1:3000/probe"])
            self.assertEqual(result, [1008, 1008])
            self.assertFalse(received.is_set())
            self.assertEqual(explorer.state["blocked_requests"], 2)
            return {}
        async def finished(*args):
            pass
        try:
            with patch.object(explorer, "_recon", side_effect=recon), patch.object(explorer, "_agent_loop", side_effect=finished):
                await asyncio.wait_for(explorer._run(0), 10)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)


if __name__ == "__main__":
    unittest.main()

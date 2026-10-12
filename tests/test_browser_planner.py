"""The generic code loop explores a small site unrelated to Juice Shop."""

import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from explorer import ExplorerV3
from session_log import SessionStore


HTML = b'''<!doctype html><title>Fixture catalog</title>
<button onclick="document.querySelector('input').hidden=false">Reveal search</button>
<input hidden aria-label="Catalog search"><div id="result"></div>
<script>document.querySelector('input').addEventListener('keydown', async e => {
  if(e.key==='Enter') { const r=await fetch('/catalog?query='+encodeURIComponent(e.target.value));
    document.querySelector('#result').textContent=JSON.stringify(await r.json()); }
});</script>'''


class Fixture(BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlsplit(self.path)
        if path.path == "/catalog":
            value = parse_qs(path.query).get("query", [""])[0]
            body = json.dumps({"items": [value]}).encode()
            content_type = "application/json"
        else:
            body, content_type = HTML, "text/html"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


def model_reply(action, **fields):
    item = {"action": action, "plan": "inspect", "reason": "observe", "path": "", "content": "",
            "command": "", "code": ""}
    item.update(fields)
    return {"message": {"content": json.dumps(item)}}


class BrowserPlannerTests(unittest.TestCase):
    def test_generated_code_controls_fixture_without_fixed_web_actions(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
        serving = threading.Thread(target=server.serve_forever, daemon=True)
        serving.start()
        target = f"http://127.0.0.1:{server.server_port}/"
        first = f"await page.goto('{target}')\nprint(await page.title())"
        second = "await page.get_by_text('Reveal search').click()\nawait page.get_by_label('Catalog search').fill('Pear')\nawait page.get_by_label('Catalog search').press('Enter')\nawait page.wait_for_selector('#result:not(:empty)')\nprint(await page.locator('#result').inner_text())"
        answers = iter([model_reply("run_python", code=first), model_reply("run_python", code=second),
                        model_reply("finish")])
        try:
            with tempfile.TemporaryDirectory() as temp:
                def transport(_payload):
                    yield next(answers)
                explorer = ExplorerV3(Path(temp), transport=transport, lab_reset=lambda: {"verified": True},
                                      allowed_target=target)
                run_id = explorer.start(target=target, goal="Inspect the fixture catalog")
                explorer.worker.join(25)
                self.assertFalse(explorer.worker.is_alive())
                meta = SessionStore(Path(temp)).read(run_id)
                self.assertEqual(meta["run_result"], "completed", meta.get("finish_reason"))
                events = [json.loads(line) for line in (Path(temp) / run_id / "events.jsonl").read_text(encoding="utf-8").splitlines()]
                self.assertTrue(any(e.get("kind") == "exec_output" and "Fixture catalog" in e.get("text", "") for e in events))
                self.assertTrue(any(e.get("kind") == "exec_output" and "Pear" in e.get("text", "") for e in events))
                self.assertFalse((Path(temp) / run_id / "runtime").exists())
        finally:
            server.shutdown()
            server.server_close()
            serving.join(2)


if __name__ == "__main__":
    unittest.main()

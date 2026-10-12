import io
import json
import unittest
from unittest.mock import patch

import app


class ScopeTests(unittest.TestCase):
    def test_only_juice_shop_origin_is_allowed(self):
        self.assertEqual(app.normalize_url("/api/Products"), "http://127.0.0.1:3000/api/Products")
        for raw in (
            "http://example.com/",
            "http://127.0.0.1:3001/",
            "http://127.0.0.1:3000.evil.example/",
            "javascript:alert(1)",
            "//example.com/api/Products",
            "http://127.0.0.1:3000/\nwhoami",
        ):
            with self.subTest(raw=raw):
                self.assertIsNone(app.normalize_url(raw))

    def test_html_and_javascript_discovery(self):
        html = '<a href="/login">Login</a><script src="main.js"></script><form action="/search"><input name="q"></form>'
        found, inputs = app.extract_candidates(html, "text/html", app.TARGET)
        self.assertEqual({item["url"] for item in found}, {
            "http://127.0.0.1:3000/login",
            "http://127.0.0.1:3000/main.js",
            "http://127.0.0.1:3000/search",
        })
        self.assertEqual(inputs, ["q"])
        js = 'this.host=`/api/Products`; route={path:`basket`}; const x=`/rest/user/whoami`'
        found, _ = app.extract_candidates(js, "application/javascript", app.TARGET + "main.js")
        self.assertIn("http://127.0.0.1:3000/api/Products", {item["url"] for item in found})
        self.assertIn("http://127.0.0.1:3000/basket", {item["url"] for item in found})

    def test_spa_fallback_is_not_confirmed(self):
        same = app.FetchResult(200, "text/html", "same", "abc", 0.1)
        confirmed, fallback = app.is_confirmed(same, "route", "abc", False)
        self.assertFalse(confirmed)
        self.assertTrue(fallback)
        api_html = app.FetchResult(200, "text/html", "different", "def", 0.1)
        confirmed, _ = app.is_confirmed(api_html, "api", "abc", False)
        self.assertFalse(confirmed)
        api_json = app.FetchResult(200, "application/json", "{}", "ghi", 0.1)
        confirmed, _ = app.is_confirmed(api_json, "api", "abc", False)
        self.assertTrue(confirmed)

    def test_ollama_choice_uses_structured_local_request(self):
        pieces = ['{"reason":"확인",', '"action":"visit","candidate_id":0}']
        fake = io.BytesIO(b"".join(json.dumps({"message": {"content": piece}}).encode() + b"\n" for piece in pieces))
        candidates = [{"url": "http://127.0.0.1:3000/main.js", "kind": "script"}]
        received = []
        with patch("app.urlopen", return_value=fake) as opened:
            candidate_id, reason, fallback, output = app.choose_with_ollama(candidates, [], 3, on_text=received.append)
        self.assertEqual((candidate_id, reason, fallback), (0, "확인", False))
        self.assertEqual(received, pieces)
        self.assertEqual(output, "".join(pieces))
        sent = json.loads(opened.call_args.args[0].data)
        self.assertEqual(sent["model"], "qwen3.5:2b")
        self.assertIs(sent["think"], False)
        self.assertIs(sent["stream"], True)

    def test_console_subscriber_receives_reset_and_deltas(self):
        explorer = app.Explorer()
        subscriber, initial = explorer.subscribe()
        self.assertEqual(initial, "")
        explorer._append_console("첫 출력")
        self.assertEqual(subscriber.get_nowait(), ("delta", "첫 출력"))
        self.assertEqual(explorer.snapshot()["console"], "첫 출력")
        explorer.unsubscribe(subscriber)


if __name__ == "__main__":
    unittest.main()

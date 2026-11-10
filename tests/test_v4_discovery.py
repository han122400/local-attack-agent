import unittest

from engine.contracts import validate_discovery
from engine.discovery import analyze_response


SOURCE = "https://shop.example/account/start"


class DiscoveryTests(unittest.TestCase):
    def test_form_fields_keep_their_form_method_and_default_action(self):
        body = '''<form><input name="query"><input name="ignored" disabled></form>
          <form action="/sessions" method="post"><input name="email"><input name="password"></form>'''
        result = analyze_response(body, SOURCE, content_type="text/html")
        found = {(item["locator"]["url"], item["locator"]["method"], item["locator"]["input"])
                 for item in result["surfaces"]}
        self.assertEqual(found, {
            ("https://shop.example/account/start?query=", "GET", "query"),
            ("https://shop.example/sessions", "POST", "email"),
            ("https://shop.example/sessions", "POST", "password"),
        })
        for item in result["surfaces"]:
            self.assertEqual(validate_discovery(item)["kind"], "surface")
            self.assertIsNone(item["hypothesis"])

    def test_base_assets_and_query_links_are_kept_separate(self):
        body = '''<base href="/portal/"><script type="module" src="./entry.js"></script>
            <a href="/lookup?term=blue&amp;sort=asc">find</a><a href="#/settings">settings</a>'''
        result = analyze_response(body, SOURCE, content_type="text/html")
        self.assertEqual(result["assets"], [{"url": "https://shop.example/portal/entry.js", "kind": "module"}])
        self.assertEqual({item["locator"]["input"] for item in result["surfaces"]}, {"term", "sort"})
        self.assertTrue(all(item["locator"]["url"] == "https://shop.example/lookup?sort=&term="
                            for item in result["surfaces"]))
        self.assertTrue(any(hint["kind"] == "client_route" for hint in result["hints"]))

    def test_external_urls_and_non_http_urls_are_not_local_inputs(self):
        body = '''<script src="//cdn.example/app.js"></script>
            <form action="https://elsewhere.example/login"><input name="secret"></form>
            <a href="javascript:alert(1)">x</a><a href="https://user:pass@shop.example/lookup?q=x">x</a>
            <a href="/lookup?q=x">local</a>'''
        result = analyze_response(body, SOURCE, content_type="text/html")
        self.assertEqual(result["assets"], [])
        self.assertEqual([item["locator"]["url"] for item in result["surfaces"]],
                         ["https://shop.example/lookup?q="])

    def test_external_base_does_not_turn_relative_assets_into_local_assets(self):
        result = analyze_response('<base href="https://cdn.example/"><script src="app.js"></script>',
                                  SOURCE, content_type="text/html")
        self.assertEqual(result["assets"], [])

    def test_static_http_calls_preserve_methods_and_declared_parameters(self):
        body = '''fetch("/v2/lookup?term=blue");
            axios.post('/v2/session', {email: user.email, password});
            this.http.get('/v2/items', {params: {category: name, page: 1}});'''
        result = analyze_response(body, "https://shop.example/assets/app.js", content_type="application/javascript")
        found = {(item["locator"]["url"], item["locator"]["method"], item["locator"]["input"])
                 for item in result["surfaces"]}
        self.assertEqual(found, {
            ("https://shop.example/v2/lookup?term=", "GET", "term"),
            ("https://shop.example/v2/session", "POST", "email"),
            ("https://shop.example/v2/session", "POST", "password"),
            ("https://shop.example/v2/items?category=&page=", "GET", "category"),
            ("https://shop.example/v2/items?category=&page=", "GET", "page"),
        })

    def test_static_endpoint_without_known_inputs_is_labeled_as_a_declaration(self):
        result = analyze_response('fetch("/v2/catalog");', SOURCE, content_type="text/javascript")
        self.assertEqual(result["surfaces"][0]["locator"]["input"], "")
        hint = next(item for item in result["hints"] if item["kind"] == "request")
        self.assertFalse(hint["input_known"])
        self.assertTrue(hint["declared_only"])
        self.assertEqual(hint["source_url"], SOURCE)

    def test_dynamic_origin_and_path_are_hints_without_inventing_a_real_url(self):
        body = '''this.http.get(`${server}/v2/lookup?term=${term}`);
            this.http.get(server+`/v2/notify`, {params:{topic:value}});
            fetch(`/v2/users/${id}?detail=1`);'''
        result = analyze_response(body, SOURCE, content_type="text/javascript")
        self.assertEqual(result["surfaces"], [])
        hints = [item for item in result["hints"] if item["kind"] == "dynamic_request"]
        self.assertEqual(len(hints), 3)
        self.assertTrue(any(item.get("origin_unknown") and "term" in item["inputs"] for item in hints))
        self.assertTrue(any(item.get("probe_url") == "https://shop.example/v2/lookup?term="
                            and item.get("origin_assumed") for item in hints))
        self.assertTrue(any(item.get("path_hint", "").startswith("/v2/notify") and "topic" in item["inputs"]
                            for item in hints))
        self.assertTrue(any(item.get("reason") == "dynamic_path" for item in hints))
        self.assertTrue(all(not item.get("url") for item in hints))

    def test_dynamic_query_values_do_not_hide_known_input_names(self):
        result = analyze_response('fetch(`/v2/lookup?term=${term}`);', SOURCE, content_type="text/javascript")
        self.assertEqual(result["surfaces"][0]["locator"]["url"], "https://shop.example/v2/lookup?term=")
        self.assertEqual(result["surfaces"][0]["locator"]["input"], "term")

    def test_comments_bare_paths_and_css_are_not_promoted_to_surfaces(self):
        body = '''// fetch("/fake?secret=1")
            const route="/administration"; const icon="/assets/icon.svg";
            /* axios.post('/fake-post', {credential:x}); */
            const theme="margin:0; color:red";'''
        result = analyze_response(body, SOURCE, content_type="text/javascript")
        self.assertEqual(result["surfaces"], [])
        self.assertTrue(any(item["kind"] == "url_literal" and item.get("path_hint") == "/administration"
                            for item in result["hints"]))
        self.assertFalse(any("fake" in str(item) for item in result["hints"]))

    def test_js_modules_are_analysis_assets(self):
        result = analyze_response('import "./entry.js"; import("./chunk.mjs");',
                                  "https://shop.example/assets/bootstrap.js", content_type="text/javascript")
        self.assertEqual({item["url"] for item in result["assets"]},
                         {"https://shop.example/assets/entry.js", "https://shop.example/assets/chunk.mjs"})
        self.assertEqual(result["surfaces"], [])

    def test_application_module_is_listed_before_support_scripts(self):
        html = ('<script type="module" src="/polyfills.js"></script>'
                '<script src="/scripts.js"></script>'
                '<script type="module" src="/main.js"></script>')
        result = analyze_response(html, SOURCE, content_type="text/html")
        self.assertEqual(result["assets"][0]["url"], "https://shop.example/main.js")

    def test_large_html_retains_script_references_at_the_end_and_reports_truncation(self):
        body = '<html><head></head><body>' + 'x' * 2_200_000 + '<script src="/assets/entry.js"></script></body></html>'
        result = analyze_response(body, SOURCE, content_type="text/html")
        self.assertIn({"url": "https://shop.example/assets/entry.js", "kind": "script"}, result["assets"])
        self.assertTrue(result["truncated"])

    def test_result_limits_and_source_validation(self):
        result = analyze_response('<form><input name="one"><input name="two"></form>',
                                  SOURCE, content_type="text/html", limit=1)
        self.assertEqual(len(result["surfaces"]), 1)
        self.assertTrue(result["truncated"])
        for source, allowed in [("file:///tmp/a", None), (SOURCE, "https://different.example/"),
                                ("https://user:pass@shop.example/", None), ("https://shop.example:0/", None)]:
            with self.subTest(source=source, allowed=allowed), self.assertRaises(ValueError):
                analyze_response("", source, allowed_origin=allowed)
        for limit in [0, True, 101]:
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                analyze_response("", SOURCE, limit=limit)

    def test_long_hint_values_and_input_lists_are_bounded(self):
        result = analyze_response('const path="/' + 'x' * 8000 + '";', SOURCE, content_type="text/javascript")
        self.assertTrue(result["truncated"])
        self.assertLessEqual(len(result["hints"][0]["path_hint"]), 2048)
        result = analyze_response('<form>' + ''.join(f'<input name="field{i}">' for i in range(30)) + '</form>',
                                  SOURCE, content_type="text/html", limit=2)
        self.assertLessEqual(len(result["hints"][0]["inputs"]), 2)
        self.assertTrue(result["truncated"])

    def test_malformed_js_url_does_not_abort_analysis_of_other_calls(self):
        result = analyze_response('fetch("https://["); fetch("/v2/search?q=one");',
                                  SOURCE, content_type="text/javascript")
        self.assertEqual(result["surfaces"][0]["locator"]["url"], "https://shop.example/v2/search?q=")

    def test_regex_quotes_and_nested_templates_do_not_hide_later_requests(self):
        code = "const pattern=/['\"`]/; const html=`outer ${flag ? `nested ${value}` : 'x'}`; fetch(`/v2/lookup?term=${term}`);"
        result = analyze_response(code, SOURCE, content_type="text/javascript")
        self.assertEqual(len(result["surfaces"]), 1)
        self.assertEqual(result["surfaces"][0]["locator"]["url"], "https://shop.example/v2/lookup?term=")

    def test_javascript_bundle_signature_is_recognized_when_evidence_was_saved_as_text(self):
        body = 'import{value}from"./chunk.js"; this.http.get(server+`/v2/lookup?term=${value}`);'
        result = analyze_response(body, SOURCE, content_type="text/plain")
        self.assertTrue(any(item.get("path_hint") == "/v2/lookup?term=${value}" and
                            "term" in item["inputs"] for item in result["hints"]))
        self.assertEqual(result["surfaces"], [])


if __name__ == "__main__":
    unittest.main()

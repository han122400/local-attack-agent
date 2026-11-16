"""Fixed browser actions; raw URLs and captured requests stay in the worker."""

import hashlib
import uuid
from html.parser import HTMLParser
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit

from j25_verifiers import verify_candidate
from web_discovery import analyze_response


class _Links(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []
        self.has_text = False
        self.hidden = 0
        self.texts = []
        self.text_length = 0
        self.label_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "head"}:
            self.hidden += 1
        if tag in {"h1", "h2", "h3", "label", "button"}:
            self.label_depth += 1
        if tag == "a":
            href = dict(attrs).get("href")
            if href and len(self.links) < 128:
                self.links.append(href)

    def handle_endtag(self, tag):
        if tag in {"script", "style", "head"} and self.hidden:
            self.hidden -= 1
        if tag in {"h1", "h2", "h3", "label", "button"} and self.label_depth:
            self.label_depth -= 1

    def handle_data(self, data):
        if not self.hidden and data.strip():
            self.has_text = True
            if self.label_depth and self.text_length + len(data) <= 10_000:
                self.texts.append(data.strip())
                self.text_length += len(data)


def _public_url(url):
    parts = urlsplit(url)
    fragment = parts.fragment.partition("?")[0] if parts.fragment.startswith("/") else ""
    return urlunsplit((parts.scheme, parts.netloc.rsplit("@", 1)[-1], parts.path, "", fragment))[:300]


class J25Actions:
    def __init__(self, target, page, http, goto, list_inputs, progress=None, stop_requested=None,
                 proof_canaries=None, identity="public"):
        self.target, self.page, self.http = target, page, http
        self.proof_canaries = proof_canaries or {}
        self.identity = identity
        self.http._remember_values({item["value"] for item in self.proof_canaries.values()})
        self._remember_url(target)
        self.goto, self.list_inputs = goto, list_inputs
        self.progress = progress or (lambda value: None)
        self.stop_requested = stop_requested or (lambda: False)
        self.prefix = uuid.uuid4().hex[:12]
        self.items = {"pages": {}, "assets": {}, "inputs": {}}
        self.keys = {"pages": {}, "assets": {}, "inputs": {}}
        self.omitted = {"pages": 0, "assets": 0, "inputs": 0}
        self.current = {}
        self.page_status = None
        self.root = self._add("pages", (target,), {"url": target, "source": "target"})

    def _add(self, kind, key, item):
        if key in self.keys[kind]:
            ref = self.keys[kind][key]
            if item.get("basis") == "observed":
                self.items[kind][ref]["source"] = item["source"]
            elif item.get("basis") == "visible":
                self.items[kind][ref].update(item)
            return ref
        if len(self.items[kind]) >= 128:
            self.omitted[kind] += 1
            return ""
        ref = f"{kind[0].upper()}{self.prefix}-{len(self.items[kind]) + 1}"
        self.keys[kind][key] = ref
        self.items[kind][ref] = item
        return ref

    def _allowed(self, url):
        return isinstance(url, str) and self.http._block_reason(url, "GET") is None

    def _remember_url(self, url):
        self.http._remember_get_secrets(url, {})
        fragment_query = urlsplit(url).fragment.partition("?")[2]
        if fragment_query:
            self.http._remember_get_secrets(urlunsplit(("https", "redaction.invalid", "/", fragment_query, "")), {})

    def _page(self, url, source):
        if self._allowed(url):
            self._remember_url(url)
            self._add("pages", (url,), {"url": url, "source": source})

    def _scan(self, body, source_url, source_ref, content_type, *, source_truncated=False):
        result = analyze_response(body, source_url, content_type=content_type, allowed_origin=self.target)
        truncated = bool(result.get("truncated") or source_truncated)
        for item in result["assets"]:
            url = item.get("url")
            if self._allowed(url):
                self._remember_url(url)
                self._add("assets", (url,), {"url": url, "source": source_ref, "kind": item.get("kind", "script")})
        for item in result["hints"]:
            if item.get("kind") == "navigation":
                self._page(item.get("url"), source_ref)
            elif item.get("kind") == "client_route":
                self._page(urljoin(source_url, item.get("path_hint", "")), source_ref)
        groups = {}
        for item in result["surfaces"]:
            loc = item.get("locator", {})
            method = loc.get("method")
            if method in {"GET", "POST"} and self._allowed(loc.get("url")):
                groups.setdefault((loc["url"], method), set()).add(loc.get("input", ""))
        for (url, method), fields in groups.items():
            supported = method == "GET" and len(fields) == 1 and "" not in fields and not truncated
            limitation = ("" if supported else "unsupported_method" if method != "GET" else
                          "truncated_declaration" if truncated else "baseline_unknown")
            for field in fields:
                self._add("inputs", ("declared", url, method, field), {"url": url, "field": field,
                          "method": method, "supported": supported, "limitation": limitation,
                          "basis": "declared", "source": source_ref})
        if "html" in content_type:
            parser = _Links()
            parser.feed(body[:2_000_000])
            for href in parser.links:
                try:
                    self._page(urljoin(source_url, href), source_ref)
                except ValueError:
                    continue
        return truncated

    def _captured(self):
        requests = self.http.captured_gets()
        forms = self.http.captured_requests()
        active = {request["id"] for request in [*requests, *forms]}
        for item in self.items["inputs"].values():
            if item.get("basis") == "observed" and item.get("request_id") not in active:
                item.update(supported=False, limitation="request_expired")
        for request in requests:
            for field in request["fields"]:
                self._add("inputs", ("observed", request["id"], field), {"url": request["url"], "field": field,
                          "method": "GET", "supported": field not in request["ambiguous"],
                          "replayable": field not in request["ambiguous"],
                          "limitation": "ambiguous_field" if field in request["ambiguous"] else "",
                          "basis": "observed", "request_id": request["id"], "source": request["step"]})
        for request in forms:
            for field in request.get("fields") or []:
                replayable = (self.http.allowed_mutation_paths is not None
                              and self.http._block_reason(request["url"], request["method"]) is None)
                self._add("inputs", ("observed", request["id"], field), {"url": request["url"],
                          "field": field, "method": request["method"], "supported": replayable,
                          "replayable": replayable,
                          "limitation": "" if replayable else "mutation_scope_required",
                          "basis": "observed", "request_id": request["id"], "source": request["step"]})

    def references(self):
        self._captured()
        return {kind: [{"id": ref, **{k: (self._reference_url(v) if k == "url" else
                                          self.http.redact_get_secrets(v) if k == "field" else v)
                    for k, v in item.items() if k != "request_id"}} for ref, item in items.items()]
                for kind, items in self.items.items()}

    def _reference_url(self, url):
        parts = urlsplit(_public_url(url))
        return urlunsplit((parts.scheme, parts.netloc, self.http.redact_get_secrets(parts.path),
                           "", self.http.redact_get_secrets(parts.fragment)))

    def _sample(self, result, stage):
        body = result.get("body", "")
        if not isinstance(body, str):
            body = ""
        return {"stage": stage, "status": result.get("status"), "length": result.get("length"),
                "body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
                "truncated": bool(result.get("body_truncated")), "binary": bool(result.get("binary")),
                "canary_hits": sorted(label for label, item in self.proof_canaries.items()
                    if not result.get("body_truncated") and not result.get("binary") and item["value"] in body)}

    async def _fill_visible(self, item, value):
        return await self.page.evaluate("""([index, value, name, id, type]) => {
            const visible = el => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
                return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
            const el = [...document.querySelectorAll('input,textarea,select')].filter(visible)[index];
            if (!el || (el.getAttribute('name') || '') !== name || el.id !== id
                || (el.getAttribute('type') || '') !== type) return false;
            el.focus(); el.value = value;
            el.dispatchEvent(new Event('input', { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
            return true;
        }""", [item["index"], value, item["name"], item["element_id"], item["type"]])

    async def _dom_sample(self, stage):
        body = await self.page.content()
        return self._sample({"status": self.page_status, "body": body,
                             "length": len(body), "body_truncated": len(body) > 2_000_000}, stage)

    async def perform(self, decision):
        action = decision.get("action")
        ref = self.root if action == "start" else decision.get("ref")
        self.current = {"action": action, "ref": ref, "status": "incomplete", "request_count": 0}
        try:
            if self.stop_requested():
                raise InterruptedError("run stopped")
            if action in {"start", "observe"}:
                item = self.items["pages"].get(ref)
                if item is None or not self._allowed(item["url"]):
                    raise ValueError("unknown or expired page")
                await self.goto(item["url"])
                self.page_status = self.http.last_navigation_status
                body = await self.page.content()
                self.current["truncated"] = self._scan(body, self.page.url, ref, "text/html")
                for existing in self.items["inputs"].values():
                    if existing.get("basis") == "visible" and existing.get("url") != self.page.url:
                        existing.update(interactable=False, limitation="input_expired")
                inputs = await self.list_inputs()
                rows = inputs.get("inputs", [])[:30]
                self.current["visible_inputs"] = [{key: self.http.redact_get_secrets(str(row.get(key, "")))[:100]
                    for key in ("tag", "type", "name", "id", "label")} for row in rows]
                for index, row in enumerate(rows):
                    field = str(row.get("name") or row.get("id") or f"visible-{index + 1}")[:100]
                    self._add("inputs", ("visible", ref, index, field), {"url": self.page.url, "field": field,
                        "index": index, "name": str(row.get("name") or "")[:100],
                        "element_id": str(row.get("id") or "")[:100], "type": str(row.get("type") or "")[:100],
                        "method": "unknown", "supported": False, "interactable": True,
                        "limitation": "unresolved_visible_input", "basis": "visible", "source": ref})
                parser = _Links()
                parser.feed(body[:2_000_000])
                self.current["page_excerpt"] = self.http.redact_get_secrets(" ".join(parser.texts))[:1000]
                has_details = bool(self.current["visible_inputs"] or any(item.get("source") == ref
                    for kind in ("assets", "inputs", "pages") for item in self.items[kind].values()))
                status = self.http.last_navigation_status
                self.current["navigation"] = {"status": status, "blocked_requests": len(self.http.blocked_requests)}
                if status is not None and status >= 400:
                    self.current["error"] = "navigation_http_error"
                elif not parser.has_text and not has_details:
                    self.current["error"] = "empty_page_observation"
            elif action == "analyze":
                item = self.items["assets"].get(ref)
                if item is None or not self._allowed(item["url"]):
                    raise ValueError("unknown or expired asset")
                self.current["request_count"] = 1
                response = await self.http.http_get(item["url"])
                self.current["response"] = self._sample(response, "asset")
                if not 200 <= response.get("status", 0) < 300 or response.get("binary"):
                    raise ValueError("asset response unavailable")
                self.current["truncated"] = self._scan(response.get("body", ""), item["url"], ref,
                    response.get("content_type", ""), source_truncated=bool(response.get("body_truncated")))
            elif action in {"compare", "verify"}:
                item = self.items["inputs"].get(ref)
                normal, test = decision.get("normal"), decision.get("test")
                if (item is None or not item.get("supported") or not all(isinstance(x, str) and x and len(x) <= 500 for x in (normal, test))
                        or normal == test):
                    raise ValueError("invalid or expired input")
                self.current["basis"] = item["basis"]
                self.current["comparison"] = {"samples": []}
                for stage, value in (("normal", normal), ("test", test), ("recheck", normal)):
                    self.current["comparison"]["failed_stage"] = stage
                    if self.stop_requested():
                        raise InterruptedError("run stopped")
                    self.current["request_count"] += 1
                    if item["basis"] == "observed":
                        response = (await self.http.replay_get(item["request_id"], item["field"], value)
                            if item["method"] == "GET" else await self.http.replay_request(
                                item["request_id"], {item["field"]: value}))
                    else:
                        parts = urlsplit(item["url"])
                        url = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode({item["field"]: value}), ""))
                        response = await self.http.http_get(url)
                    self.current["comparison"]["samples"].append(self._sample(response, stage))
                    self.progress(self.current)
                samples = self.current["comparison"]["samples"]
                self.current["comparison"].pop("failed_stage")
                self.current["comparison"]["baseline_stable"] = None if any(
                    sample["truncated"] or sample["binary"] for sample in (samples[0], samples[2])) else (
                    samples[0]["status"], samples[0]["body_sha256"], samples[0]["truncated"]
                ) == (samples[2]["status"], samples[2]["body_sha256"], samples[2]["truncated"])
                self.current["comparison"]["test_differs"] = None if any(
                    sample["truncated"] or sample["binary"] for sample in (samples[0], samples[1])) else (
                    samples[0]["status"], samples[0]["body_sha256"]
                ) != (samples[1]["status"], samples[1]["body_sha256"])
                if action == "verify":
                    self.current["verification"] = verify_candidate(decision.get("kind"), {
                        "samples": samples, "test_value": test, "identity": self.identity,
                        "method": item["method"],
                        "test_contains_canary": any(spec["value"] in test for spec in self.proof_canaries.values())},
                        scope={"canaries": {label: {"protected": spec["protected"],
                            "authorized_identities": spec["authorized_identities"]}
                            for label, spec in self.proof_canaries.items()},
                            "mutation_allowed": bool(self.http.allowed_mutation_paths)})
            elif action == "verify_browser":
                item = self.items["inputs"].get(ref)
                if item is None or not item.get("interactable") or item["url"] != self.page.url:
                    raise ValueError("unknown or stale visible input")
                nonce = uuid.uuid4().hex
                property_name = f"__j25_probe_{nonce}"
                normal = "j25-normal"
                payload = (f"<img src='data:image/png;base64,invalid' "
                           f"onerror='window.{property_name}=document.body.innerText'>")
                self.current["basis"] = "visible"
                self.current["comparison"] = {"samples": []}
                execution = {"executed": False, "recheck_executed": False, "canary_hits": []}
                for stage, value in (("normal", normal), ("test", payload), ("recheck", normal)):
                    self.current["comparison"]["failed_stage"] = stage
                    if self.stop_requested():
                        raise InterruptedError("run stopped")
                    await self.page.evaluate("name => { delete window[name] }", property_name)
                    if not await self._fill_visible(item, value):
                        raise ValueError("visible input changed")
                    await self.page.wait_for_timeout(250)
                    proof_text = await self.page.evaluate("name => window[name] || ''", property_name)
                    if stage == "test":
                        execution["executed"] = isinstance(proof_text, str) and bool(proof_text)
                        execution["canary_hits"] = sorted(label for label, spec in self.proof_canaries.items()
                            if spec["value"] in proof_text)
                    elif stage == "recheck":
                        execution["recheck_executed"] = bool(proof_text)
                    self.current["comparison"]["samples"].append(await self._dom_sample(stage))
                    self.progress(self.current)
                samples = self.current["comparison"]["samples"]
                self.current["comparison"].pop("failed_stage")
                self.current["comparison"]["baseline_stable"] = (
                    samples[0]["status"], samples[0]["body_sha256"]) == (
                    samples[2]["status"], samples[2]["body_sha256"])
                self.current["comparison"]["test_differs"] = (
                    samples[0]["status"], samples[0]["body_sha256"]) != (
                    samples[1]["status"], samples[1]["body_sha256"])
                self.current["verification"] = verify_candidate("browser_input", {
                    "samples": samples, "identity": self.identity, "execution_proof": execution,
                    "test_contains_canary": False}, scope={"canaries": {label: {
                        "protected": spec["protected"], "authorized_identities": spec["authorized_identities"]}
                        for label, spec in self.proof_canaries.items()}})
            elif action in {"interact", "submit"}:
                item = self.items["inputs"].get(ref)
                value = decision.get("value")
                if (item is None or not item.get("interactable") or item["url"] != self.page.url
                        or not isinstance(value, str) or not 0 < len(value) <= 500):
                    raise ValueError("unknown or stale visible input")
                if action == "submit" and not self.http.allowed_mutation_paths:
                    raise ValueError("form submission requires mutation test scope")
                filled = await self._fill_visible(item, value)
                self.current["interaction"] = {"filled": bool(filled), "submitted": False}
                if not filled:
                    raise ValueError("visible input changed")
                if action == "submit":
                    before = self.page.url
                    await self.page.keyboard.press("Enter")
                    await self.page.wait_for_timeout(1200)
                    self.current["interaction"].update(submitted=True, url_changed=self.page.url != before)
                    self.current["truncated"] = self._scan(await self.page.content(), self.page.url, ref, "text/html")
                    if any(blocked.get("reason") in {"method_not_in_test_scope", "path_not_in_test_scope",
                                                          "mutation_redirect_blocked", "mutation_transport_error"}
                           for blocked in self.http.blocked_requests):
                        self.current["error"] = "submission_blocked_by_scope"
            elif action == "replay":
                item = self.items["inputs"].get(ref)
                value = decision.get("value")
                if (item is None or not item.get("replayable")
                        or not isinstance(value, str) or not 0 < len(value) <= 500):
                    raise ValueError("unknown or unauthorized request")
                self.current["request_count"] += 1
                response = (await self.http.replay_get(item["request_id"], item["field"], value)
                            if item["method"] == "GET" else await self.http.replay_request(
                                item["request_id"], {item["field"]: value}))
                self.current["response"] = self._sample(response, "replay")
            else:
                raise ValueError("unsupported fixed action")
            if self.current.get("truncated") and not self.current.get("error"):
                self.current["error"] = "source_truncated"
            self.current["status"] = "incomplete" if self.current.get("error") else "ok"
        except Exception as exc:
            self.current["error"] = type(exc).__name__
        self.current["references"] = self.references()
        self.current["omitted"] = dict(self.omitted)
        return self.current

import ast
import asyncio
import builtins
import contextlib
import inspect
import json
import os
import socket
import sys
import traceback
import types
from pathlib import Path
from urllib.parse import urlsplit

project = Path(sys.argv[5])
sys.path.insert(0, str(project / ".deps"))
from playwright.async_api import async_playwright
from browser_http import BrowserHTTP
from j2_policy import CandidateNotebook, bounded_discovery
from j25_actions import J25Actions
from j25_verifiers import verify_candidate
from web_discovery import analyze_response

def send(sock, item):
    sock.sendall((json.dumps(item, ensure_ascii=False) + "\n").encode("utf-8"))

def recv(sock, buffer):
    while b"\n" not in buffer:
        chunk = sock.recv(65536)
        if not chunk:
            raise EOFError("controller disconnected")
        buffer += chunk
    line, buffer = buffer.split(b"\n", 1)
    return json.loads(line), buffer

async def main():
    host, port, token, target, project_root, executable = sys.argv[1:7]
    is_j25 = len(sys.argv) > 7 and sys.argv[7] == "J2.5"
    test_scope = None
    sock = socket.create_connection((host, int(port)), timeout=20)
    sock.settimeout(None)
    send(sock, {"type": "hello", "token": token})
    if is_j25:
        setup, _ = recv(sock, b"")
        if setup.get("type") != "j25_target" or not isinstance(setup.get("target"), str):
            raise ValueError("J2.5 target setup failed")
        target = setup["target"]
        test_scope = setup.get("test_scope")
    target_parts = urlsplit(target)
    async with async_playwright() as playwright:
        options = {"headless": True}
        if executable:
            options["executable_path"] = executable
        browser = await playwright.chromium.launch(**options)
        context = await browser.new_context(service_workers="block", accept_downloads=False)
        is_j2 = len(sys.argv) > 7 and sys.argv[7] == "J2"
        http = BrowserHTTP(context, target, body_limit=2_000_000 if is_j2 or is_j25 else 2000,
                           capture_gets=is_j25,
                           allowed_methods=({"GET", "HEAD", "OPTIONS"} | set(test_scope["allowed_methods"])
                               if is_j25 and test_scope else {"GET", "HEAD", "OPTIONS"} if is_j25 else None),
                           allowed_mutation_paths=set(test_scope["allowed_paths"]) if is_j25 and test_scope else
                               set() if is_j25 else None)
        await context.route("**/*", http.guard)
        await context.route_web_socket("**/*", http.guard_web_socket)
        page = await context.new_page()
        await http._ensure_browser_scope(page)
        target_origin = target_parts.scheme + "://" + target_parts.netloc
        async def get_text(url):
            """GET a URL within the target origin and return its response text."""
            http._check(url, "GET")
            response = await context.request.get(url, max_redirects=0)
            http.record_response(response)
            return await response.text()
        async def get_json(url):
            """GET a URL within the target origin and return parsed JSON."""
            http._check(url, "GET")
            response = await context.request.get(url, max_redirects=0)
            http.record_response(response)
            return await response.json()
        def discover_inputs(body, source_url, content_type=""):
            result = analyze_response(body, source_url, content_type=content_type, allowed_origin=target)
            if is_j2:
                summary = bounded_discovery({"source_url": source_url, **result})
                if summary is not None:
                    discovery_summaries.append(summary)
                    if len(discovery_summaries) > 4:
                        del discovery_summaries[0]
                        summary["truncated"] = True
            return result
        async def goto_and_render(url, timeout=15000, settle_ms=1200):
            """Navigate within the target origin and let the SPA route render.

            Uses domcontentloaded + a short settle wait. networkidle is avoided on purpose:
            this app keeps polling, so networkidle may never fire and would hang until timeout.
            """
            try:
                http._check(url, "GET")
                response = await page.goto(url, wait_until="domcontentloaded", timeout=timeout)
                if is_j25:
                    http.last_navigation_status = response.status if response is not None else None
                await page.wait_for_timeout(settle_ms)
            except Exception as exc:
                if is_j2 and len(helper_errors) < 16:
                    helper_errors.append({"helper": "goto_and_render", "error_type": type(exc).__name__,
                                          "url": http._public_url(url)})
                raise
            if is_j2 and len(helper_outcomes) < 16:
                helper_outcomes.append({"helper": "goto_and_render", "outcome": "completed", "url": http._public_url(url)})
            return page
        async def list_inputs():
            """Return the page's visible inputs (index/tag/type/name/id/label/value) and nearby button
            labels, so you can pick the right field instead of guessing with a raw selector."""
            return await page.evaluate("""() => {
              const vis = el => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
                return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
              const label = el => (el.getAttribute('aria-label') || el.getAttribute('placeholder')
                || (el.labels && el.labels[0] ? el.labels[0].textContent : '') || el.getAttribute('name') || '').trim().slice(0, 80);
              const inputs = [...document.querySelectorAll('input,textarea,select')].filter(vis).slice(0, 30).map((el, i) => ({
                index: i, tag: el.tagName.toLowerCase(), type: el.getAttribute('type') || '',
                name: el.getAttribute('name') || '', id: el.id || '', label: label(el), value: (el.value || '').slice(0, 40) }));
              const buttons = [...document.querySelectorAll('button,[role=button],input[type=submit]')].filter(vis).slice(0, 20)
                .map(el => (el.getAttribute('aria-label') || el.textContent || el.value || '').trim().slice(0, 40)).filter(Boolean);
              return { inputs, buttons };
            }""")
        async def submit_input(index, value, result_selector=None, settle_ms=1200):
            """Fill the visible input at `index` (from list_inputs), submit with Enter, wait, and return a
            structured result: url before/after, title, and — if result_selector is given — the count and a
            sample of matching elements. Use the SAME result_selector for the normal and the test input to
            compare counts/content. Avoids the fragile fill/select/submit code written by hand."""
            filled = await page.evaluate("""([i, val]) => {
              const vis = el => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
                return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
              const el = [...document.querySelectorAll('input,textarea,select')].filter(vis)[i];
              if (!el) return false;
              el.focus(); el.value = val;
              el.dispatchEvent(new Event('input', { bubbles: true }));
              el.dispatchEvent(new Event('change', { bubbles: true }));
              return true;
            }""", [index, value])
            if not filled:
                return {"index": index, "value": value, "submitted": False, "error": "no visible input at index"}
            url_before = page.url
            await page.keyboard.press("Enter")
            await page.wait_for_timeout(settle_ms)
            result = {"index": index, "value": value, "submitted": True, "url_before": url_before,
                      "url_after": page.url, "url_changed": page.url != url_before, "title": await page.title()}
            if result_selector:
                found = await page.evaluate("""sel => {
                  const els = [...document.querySelectorAll(sel)];
                  return { count: els.length, sample: els.slice(0, 5).map(e => (e.textContent || '').trim().slice(0, 80)) };
                }""", result_selector)
                result["result_count"] = found["count"]
                result["result_sample"] = found["sample"]
            else:
                result["text_sample"] = await page.evaluate("() => (document.body ? document.body.innerText : '').slice(0, 400)")
            return result
        async def compare_inputs(index, normal_value, test_value, result_selector, settle_ms=1200):
            """Collect normal/test/normal with one model command; compare visible samples, never verdicts."""
            if type(index) is not int or index < 0 or not isinstance(normal_value, str) or not isinstance(test_value, str):
                raise ValueError("Use a visible input index and string input values")
            if not isinstance(result_selector, str) or not result_selector.strip():
                raise ValueError("A result_selector is required for comparison")
            if normal_value == test_value:
                raise ValueError("Normal and test values must differ")
            if type(settle_ms) is not int or settle_ms < 0:
                raise ValueError("settle_ms must be a nonnegative integer")
            samples, identity = {}, None
            def signature(field):
                return tuple(field.get(key, "") for key in ("tag", "type", "name", "id", "label"))
            for stage, value in (("normal", normal_value), ("test", test_value), ("recheck", normal_value)):
                try:
                    fields = (await list_inputs())["inputs"]
                    field = next((row for row in fields if row["index"] == index), None)
                    if field is None or (identity is not None and signature(field) != identity):
                        raise ValueError("The input disappeared or changed identity; locate it again")
                    identity = signature(field)
                    if sum(signature(row) == identity for row in fields) != 1:
                        raise ValueError("Input identity is ambiguous; compare manually")
                    sample = await submit_input(index, value, result_selector, settle_ms=settle_ms)
                    samples[stage] = sample
                    if (not sample.get("submitted") or type(sample.get("result_count")) is not int
                            or not isinstance(sample.get("result_sample"), list)):
                        raise ValueError("Submission or result sample could not be observed")
                except Exception as exc:
                    return {"status": "inconclusive", "stage": stage, "error": str(exc)[:240], "samples": samples}
            normal, test, recheck = (samples[key] for key in ("normal", "test", "recheck"))
            stable = (normal["result_count"], normal["result_sample"]) == (recheck["result_count"], recheck["result_sample"])
            observed = any(sample["result_count"] > 0 for sample in samples.values())
            return {"status": "compared" if stable and observed else "inconclusive", "comparison": {
                        "result_count_delta": test["result_count"] - normal["result_count"],
                        "sample_changed": test["result_sample"] != normal["result_sample"],
                        "baseline_sample_stable": stable},
                    "scope": "Visible result count and first five text samples only; empty samples may mean a wrong selector. Not vulnerability confirmation.",
                    "samples": samples}


        helpers = {"get_text": get_text, "get_json": get_json, "http_get": http.http_get,
                   "discover_inputs": discover_inputs,
                   "goto_and_render": goto_and_render, "list_inputs": list_inputs,
                   "submit_input": submit_input, "compare_inputs": compare_inputs, "target_origin": target_origin,
                   "captured_requests": http.captured_requests, "replay_request": http.replay_request}
        proof_canaries = {item["label"]: item for item in test_scope.get("proof_canaries", [])} if test_scope else {}
        def make_j25_actions(identity):
            return J25Actions(target, page, http, goto_and_render, list_inputs,
                progress=lambda value: send(sock, {"type": "j25_progress", "exec_id": http.step,
                    "j25_result": {**value, "request_count": value.get("request_count", 0) + http.page_request_count}}),
                stop_requested=lambda: Path("tmp/j25-cancel").exists(),
                proof_canaries=proof_canaries, identity=identity)
        j25 = make_j25_actions("public") if is_j25 else None
        sessions = {"public": (context, page, http, j25)} if is_j25 else {}
        identity_specs = {item["name"]: item for item in test_scope["identities"]} if test_scope else {}
        current_identity = "public"
        async def select_identity(name, exec_id):
            nonlocal context, page, http, j25, current_identity
            if name != "public" and name not in identity_specs:
                raise ValueError("unknown test identity")
            if name not in sessions:
                spec = identity_specs[name]
                next_context = await browser.new_context(service_workers="block", accept_downloads=False)
                try:
                    if spec["cookies"]:
                        await next_context.add_cookies(spec["cookies"])
                    next_http = BrowserHTTP(next_context, target, body_limit=2_000_000, capture_gets=True,
                        allowed_methods={"GET", "HEAD", "OPTIONS"} | set(test_scope["allowed_methods"]),
                        allowed_mutation_paths=set(test_scope["allowed_paths"]))
                    for cookie in spec["cookies"]:
                        next_http._remember_get_secrets(target, {"cookie": cookie["name"] + "=" + cookie["value"]})
                    await next_context.route("**/*", next_http.guard)
                    await next_context.route_web_socket("**/*", next_http.guard_web_socket)
                    next_page = await next_context.new_page()
                    await next_http._ensure_browser_scope(next_page)
                    context, page, http = next_context, next_page, next_http
                    sessions[name] = (context, page, http, make_j25_actions(name))
                except Exception:
                    await next_context.close()
                    raise
            context, page, http, j25 = sessions[name]
            current_identity = name
            http.begin_step(exec_id)
            return {"action": "identity", "identity": name, "ref": "", "status": "ok",
                    "request_count": 0, "references": j25.references(), "omitted": dict(j25.omitted)}
        async def verify_access(decision, exec_id):
            nonlocal context, page, http, j25, current_identity
            ref, owner_name = decision.get("ref"), decision.get("value")
            item = j25.items["pages"].get(ref)
            if (item is None or not j25._allowed(item["url"]) or owner_name == current_identity
                    or owner_name not in identity_specs):
                raise ValueError("unknown page or owner identity")
            url, unauthorized_name = item["url"], current_identity
            result = {"action": "verify_access", "identity": unauthorized_name, "ref": ref,
                      "status": "incomplete", "request_count": 0, "access_samples": []}
            j25.current = result
            previous = (context, page, http, j25, current_identity)
            try:
                for stage in ("unauthorized", "owner", "recheck"):
                    if stage == "owner":
                        await select_identity(owner_name, exec_id)
                    elif stage == "recheck":
                        context, page, http, j25, current_identity = previous
                    result["request_count"] += 1
                    sample = j25._sample(await http.http_get(url), stage)
                    result["access_samples"].append(sample)
            finally:
                context, page, http, j25, current_identity = previous
                j25.current = result
            proof_scope = {"canaries": {label: {"protected": spec["protected"],
                "authorized_identities": spec["authorized_identities"]} for label, spec in proof_canaries.items()}}
            result["verification"] = verify_candidate("authorization", {
                "identity": unauthorized_name, "owner_identity": owner_name,
                "access_samples": result["access_samples"],
                "request_contains_canary": any(spec["value"] in url for spec in proof_canaries.values())},
                scope=proof_scope)
            result.update(status="ok", references=j25.references(), omitted=dict(j25.omitted))
            return result
        send(sock, {"type": "ready"})
        buffer = b""
        try:
            while True:
                message, buffer = await asyncio.to_thread(recv, sock, buffer)
                if message.get("type") == "stop":
                    break
                if message.get("type") == "j25_execute":
                    exec_id = message["exec_id"]
                    http.begin_step(exec_id)
                    timed_out = False
                    with open(message["stdout_path"], "w", encoding="utf-8", buffering=1) as out, \
                         open(message["stderr_path"], "w", encoding="utf-8", buffering=1) as err:
                        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                            try:
                                if j25 is None:
                                    raise ValueError("J2.5 worker is unavailable")
                                decision = message["decision"]
                                task = (select_identity(decision.get("value"), exec_id)
                                        if decision.get("action") == "identity" else
                                        verify_access(decision, exec_id) if decision.get("action") == "verify_access"
                                        else j25.perform(decision))
                                value = await asyncio.wait_for(task, message["timeout"])
                            except asyncio.TimeoutError:
                                timed_out = True
                                value = {**j25.current, "status": "incomplete", "error": "TimeoutError",
                                         "references": j25.references(), "omitted": dict(j25.omitted)}
                            except Exception as exc:
                                value = {"status": "incomplete", "error": type(exc).__name__}
                    value["request_count"] = value.get("request_count", 0) + http.page_request_count
                    send(sock, {"type": "result", "exec_id": exec_id, "exit_code": 0 if value.get("status") == "ok" else 1,
                                "timed_out": timed_out, "j25_result": value,
                                "blocked_requests": [{**x, "url": http.redact_get_secrets(x["url"])} for x in http.blocked_requests],
                                "http_observations": [{**x, "url": http.redact_get_secrets(x["url"])} for x in http.http_observations]})
                    continue
                if message.get("type") != "execute":
                    continue
                exec_id = message["exec_id"]
                http.begin_step(exec_id)
                notebook = CandidateNotebook()
                discovery_summaries, helper_errors, helper_outcomes = [], [], []
                source = Path(message["path"])
                limit = message.get("timeout")
                timed_out = False
                failure_source, cause_code, exception_type = None, None, None
                with open(message["stdout_path"], "w", encoding="utf-8", buffering=1) as out, \
                     open(message["stderr_path"], "w", encoding="utf-8", buffering=1) as err:
                    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                        async def run_user_code():
                            namespace = {"__name__": "__agent__", "__file__": str(source),
                                         "browser": browser, "context": context, "page": page,
                                         "note_candidate": notebook.note_candidate,
                                         "asyncio": asyncio, **helpers}
                            modules = {}
                            for name, bindings in (("tools", {**helpers, "note_candidate": notebook.note_candidate}),
                                                   ("browser_api", {**helpers, "note_candidate": notebook.note_candidate,
                                                                    "page": page, "context": context, "browser": browser}),
                                                   ("browser", {"page": page, "context": context, "browser": browser})):
                                module = types.ModuleType(name)
                                module.__dict__.update(bindings)
                                modules[name] = module
                            def runtime_import(name, globals=None, locals=None, fromlist=(), level=0):
                                if level == 0 and name in modules:
                                    return modules[name]
                                return builtins.__import__(name, globals, locals, fromlist, level)
                            namespace["__builtins__"] = {**vars(builtins), "__import__": runtime_import}
                            tree = ast.parse(source.read_text(encoding="utf-8"), str(source))
                            # If the code already calls main() at top level, do not auto-call it again;
                            # otherwise the user's main ran twice and its output was duplicated.
                            def _is_main_call(stmt):
                                v = stmt.value.value if isinstance(getattr(stmt, "value", None), ast.Await) else getattr(stmt, "value", None)
                                return isinstance(v, ast.Call) and isinstance(v.func, ast.Name) and v.func.id == "main"
                            called_main = any(isinstance(s, ast.Expr) and _is_main_call(s) for s in tree.body)
                            compiled = compile(tree, str(source), "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
                            result = eval(compiled, namespace, namespace)
                            if inspect.isawaitable(result):
                                await result
                            entry = namespace.get("main")
                            if callable(entry) and not called_main:
                                result = entry(page, context, browser)
                                if inspect.isawaitable(result):
                                    await result
                        try:
                            # Cancel the user coroutine on timeout WITHOUT killing this worker,
                            # so the browser survives for the next command.
                            if isinstance(limit, (int, float)) and limit > 0:
                                await asyncio.wait_for(run_user_code(), limit)
                            else:
                                await run_user_code()
                            exit_code, detail = 0, ""
                        except asyncio.TimeoutError:
                            timed_out = True
                            failure_source, cause_code = "execution", "execution_timeout"
                            exit_code = None
                            detail = "Browser code timed out"
                            print(detail, file=sys.stderr, flush=True)
                        except BaseException as exc:
                            exception_type = type(exc).__name__
                            failure_source = "generated_code" if browser.is_connected() else "system"
                            cause_code = "python_exception" if browser.is_connected() else "browser_disconnected"
                            detail = traceback.format_exc()
                            print(detail, file=sys.stderr, flush=True)
                            exit_code = 1
                        out.flush()
                        err.flush()
                send(sock, {"type": "result", "exec_id": exec_id, "exit_code": exit_code,
                            "timed_out": timed_out, "exception": detail[-4000:],
                            "failure_source": failure_source, "cause_code": cause_code,
                            "exception_type": exception_type, "blocked_requests": http.blocked_requests,
                            "investigation_notes": notebook.notes,
                            "http_observations": http.http_observations,
                            "discovery_summaries": discovery_summaries, "helper_errors": helper_errors,
                            "helper_outcomes": helper_outcomes,
                            "request_templates": http.captured_requests()})
        finally:
            if is_j25:
                for active_context, *_ in sessions.values():
                    await active_context.close()
            else:
                await context.close()
            await browser.close()
            sock.close()

if __name__ == "__main__":
    asyncio.run(main())

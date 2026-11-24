"""Bounded, passive browser response audit; never literal HTTP evidence.

The browser worker also executes agent code. These parsed snapshots are useful
audit material, not independent evidence for verification or scoring.
"""

import asyncio
import base64
import json
from urllib.parse import urlsplit


MAX_RECORDS = 32
MAX_BODY_BYTES = 64 * 1024
MAX_TOTAL_BYTES = 512 * 1024
MAX_HEADER_BYTES = 16 * 1024
MAX_TASKS = 4
FINISH_SECONDS = .2


def _origin(url):
    parts = urlsplit(url)
    return parts.scheme.lower(), (parts.hostname or "").lower(), parts.port or (443 if parts.scheme == "https" else 80)


class BrowserObservationBuffer:
    def __init__(self, context, target):
        self.context, self.target = context, _origin(target)
        self._current = None
        self._requests = {}
        self._tasks = set()
        self._generation = 0
        self._closed = False
        self._handlers = {"request": self._request, "response": self._response,
                          "requestfailed": self._failed}
        for name, callback in self._handlers.items():
            context.on(name, callback)

    def begin(self, exec_id):
        if self._closed:
            return None
        if self._current:
            self._current["valid"] = False
            for task in tuple(self._current["tasks"]):
                task.cancel()
        self._requests.clear()
        self._generation += 1
        token = f"{exec_id}:{self._generation}"
        self._current = {"exec_id": exec_id, "token": token, "valid": True,
                         "admitted": 0, "tasks": set(), "records": [], "bytes": 0}
        return token

    def _request(self, request):
        try:
            state = self._current
            if (state is None or not state["valid"] or state["admitted"] >= MAX_RECORDS
                    or self.target[0] != "https" or _origin(request.url) != self.target):
                return
            # Capture request-start ownership, even if the response arrives later.
            state["admitted"] += 1
            self._requests[id(request)] = (request, state, request.url, request.method)
        except Exception:
            pass

    def _failed(self, request):
        self._requests.pop(id(request), None)

    def _response(self, response):
        try:
            owned = self._requests.pop(id(response.request), None)
            if (owned is None or owned[0] is not response.request or not owned[1]["valid"]
                    or owned[1] is not self._current or len(self._tasks) >= MAX_TASKS):
                return
            state = owned[1]
            task = asyncio.create_task(self._collect(response, state, owned[2], owned[3]))
            self._tasks.add(task)
            state["tasks"].add(task)

            def done(completed):
                self._tasks.discard(completed)
                state["tasks"].discard(completed)
                if not completed.cancelled():
                    # No observer exception is allowed to escape an event callback.
                    completed.exception()
            task.add_done_callback(done)
        except Exception:
            pass

    @staticmethod
    def _headers(raw):
        if not isinstance(raw, list):
            raise ValueError("Invalid response header metadata")
        size, result = 0, []
        for item in raw:
            name, value = item["name"], item["value"]
            if not isinstance(name, str) or not isinstance(value, str):
                raise ValueError("Invalid response header metadata")
            size += len(name.encode("utf-8")) + len(value.encode("utf-8"))
            if size > MAX_HEADER_BYTES:
                raise ValueError("Response header metadata exceeds limit")
            result.append([name, value])
        return result

    async def _collect(self, response, state, url, method):
        try:
            detail = {"status": response.status}
            snapshot = {"source": "browser_response_audit", "representation": "playwright_decoded",
                        "literal": False, "verification_eligible": False,
                        "label": "Parsed browser response; unverified; not original wire bytes.",
                        "request": {"url": url, "method": method}, "response": detail}
            try:
                headers = self._headers(await asyncio.wait_for(response.headers_array(), .15))
                detail["headers"] = headers
            except Exception:
                detail["body_omitted"] = "headers_unavailable_or_large"
                self._keep(state, snapshot)
                return
            encodings = [value.strip().lower() for key, value in headers if key.lower() == "content-encoding"]
            lengths = [value.strip() for key, value in headers if key.lower() == "content-length"]
            if any(value not in ("", "identity") for value in encodings):
                reason = "content_encoded"
            elif len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdigit():
                reason = "unknown_length"
            elif len(lengths[0]) > 10 or int(lengths[0]) > MAX_BODY_BYTES:
                reason = "size_limit"
            elif method == "HEAD":
                reason = "method_has_no_body"
            else:
                reason = None
            if reason:
                detail["body_omitted"] = reason
            else:
                try:
                    body = await asyncio.wait_for(response.body(), .15)
                    if not isinstance(body, bytes):
                        detail["body_omitted"] = "body_unavailable"
                    elif len(body) > MAX_BODY_BYTES:
                        detail["body_omitted"] = "size_limit"
                    else:
                        detail["body_base64"] = base64.b64encode(body).decode("ascii")
                except Exception:
                    detail["body_omitted"] = "body_unavailable"
            self._keep(state, snapshot)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    @staticmethod
    def _keep(state, snapshot):
        if not state["valid"] or len(state["records"]) >= MAX_RECORDS:
            return
        size = len(json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        if state["bytes"] + size <= MAX_TOTAL_BYTES:
            state["records"].append(snapshot)
            state["bytes"] += size

    async def finish(self, exec_id):
        state = self._current
        if state is None or state["exec_id"] != exec_id:
            return []
        self._current = None
        self._requests.clear()
        tasks = tuple(state["tasks"])
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=FINISH_SECONDS)
            for task in pending:
                task.cancel()
        state["valid"] = False
        # Yield cancellation delivery without awaiting potentially non-cooperative
        # user-modified tasks. Browser requests themselves are never cancelled.
        await asyncio.sleep(0)
        return state["records"]

    async def close(self):
        if self._closed:
            return
        self._closed = True
        if self._current:
            self._current["valid"] = False
        self._current = None
        self._requests.clear()
        for name, callback in self._handlers.items():
            try:
                self.context.remove_listener(name, callback)
            except Exception:
                pass
        for task in tuple(self._tasks):
            task.cancel()
        await asyncio.sleep(0)

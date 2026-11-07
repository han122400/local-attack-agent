"""Scoped HTTP helpers and explicit replay of recent browser form requests."""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from email.message import Message
from email.parser import BytesParser
from email.policy import default
from urllib.parse import quote_plus, unquote_plus, urlsplit, urlunsplit


class BrowserHTTP:
    def __init__(self, context, target):
        self.context = context
        self.origin = self._origin(target)
        self.blocked_requests = []
        self._requests = {}
        self._counter = 0
        self._request_prefix = uuid.uuid4().hex[:12]
        self._scoped_pages = {}
        self._scope_lock = asyncio.Lock()
        self.step = ""

    @staticmethod
    def _origin(url):
        parts = urlsplit(url)
        return (parts.scheme, (parts.hostname or "").encode("idna").decode("ascii"),
                parts.port if parts.port is not None else {"http": 80, "https": 443}.get(parts.scheme))

    @staticmethod
    def _public_url(url):
        if not isinstance(url, str):
            return "[invalid URL]"
        try:
            parts = urlsplit(url)
            return urlunsplit((parts.scheme, parts.netloc.rsplit("@", 1)[-1], parts.path, "", ""))
        except ValueError:
            return "[invalid URL]"

    def begin_step(self, step):
        self.step = step
        self.blocked_requests = []

    def _block_reason(self, url, method):
        if (not isinstance(url, str) or not url or "\\" in url
                or any(ord(char) <= 32 or ord(char) == 127 for char in url)):
            return "invalid_url"
        try:
            parts = urlsplit(url)
            if parts.username is not None or parts.password is not None or self._origin(url) != self.origin:
                return "outside_target_origin"
        except (ValueError, UnicodeError):
            return "invalid_url"
        if method not in {"GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE"}:
            return "unsupported_method"
        return None

    def _check(self, url, method):
        reason = self._block_reason(url, method)
        if reason:
            self._record_block(url, method, reason)
            raise ValueError(f"Blocked by harness: {reason}; target origin only")

    def _record_block(self, url, method, reason):
        item = {"url": self._public_url(url)[:240], "method": method, "reason": reason}
        if item not in self.blocked_requests:
            self.blocked_requests.append(item)
        self.blocked_requests = self.blocked_requests[-8:]

    async def _ensure_browser_scope(self, page):
        async with self._scope_lock:
            if page in self._scoped_pages:
                return
            scheme, hostname, port = self.origin
            hostname = ("[" + hostname + "]" if ":" in hostname
                        else re.sub(r"([*+?{}()\\])", r"\\\1", hostname))
            authority = hostname if port == {"http": 80, "https": 443}.get(scheme) else f"{hostname}:{port}"
            session = await self.context.new_cdp_session(page)
            await session.send("Network.enable")
            # Playwright routes only the initial URL; Chromium must also block redirect destinations.
            await session.send("Network.setBlockedURLs", {"urlPatterns": [
                {"urlPattern": f"{scheme}://{authority}/*", "block": False}], "urls": ["*"]})
            page.on("requestfailed", self._on_request_failed)
            page.once("close", lambda *_: self._scoped_pages.pop(page, None))
            self._scoped_pages[page] = session

    def _on_request_failed(self, request):
        reason = self._block_reason(request.url, request.method)
        if reason:
            self._record_block(request.url, request.method, reason)
        elif "ERR_BLOCKED_BY_CLIENT" in (request.failure or ""):
            self._record_block(request.url, request.method, "browser_scope_policy_blocked")

    async def guard(self, route):
        request = route.request
        try:
            self._check(request.url, request.method)
        except ValueError:
            await route.abort()
            return
        try:
            await self._ensure_browser_scope(request.frame.page)
        except Exception as exc:
            self._record_block(request.url, request.method, "scope_guard_unavailable")
            print("HARNESS_SCOPE_SETUP_FAILED:", type(exc).__name__, str(exc)[:160])
            await route.abort()
            return
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            try:
                headers = {k.lower(): v for k, v in (await request.all_headers()).items()}
                body = request.post_data_buffer
                # shortcut: replay bodies up to 2 MB; add streaming capture when larger uploads need replay.
                if body is not None and len(body) > 2_000_000:
                    raise ValueError("request body exceeds the 2 MB replay limit")
                self._counter += 1
                request_id = f"REQ-{self._request_prefix}-{self._counter:04d}"
                self._requests[request_id] = {"id": request_id, "url": request.url, "method": request.method,
                    "headers": headers, "body": body, "step": self.step}
                # shortcut: eight in-memory form requests; add persistence only if longer replay history is needed.
                while len(self._requests) > 8:
                    del self._requests[next(iter(self._requests))]
            except Exception as exc:
                print("HARNESS_REQUEST_CAPTURE_WARNING:", type(exc).__name__, str(exc)[:160])
        await route.continue_()

    async def guard_web_socket(self, route):
        self._record_block(route.url, "WEBSOCKET", "websocket_not_supported")
        await route.close()

    @staticmethod
    def _content_type(value):
        message = Message()
        message["content-type"] = value
        return message

    @classmethod
    def _multipart_parts(cls, body, content_type):
        boundary = cls._content_type(content_type).get_boundary()
        if not boundary:
            raise ValueError("multipart request has no boundary")
        marker = b"--" + boundary.encode("ascii")
        delimiters = list(re.finditer(rb"(?:\A|\r\n)" + re.escape(marker) + rb"(--)?(?:\r\n|\Z)", body))
        if len(delimiters) < 2 or not delimiters[-1].group(1):
            raise ValueError("incomplete multipart request")
        parts = []
        for opening, closing in zip(delimiters, delimiters[1:]):
            raw = body[opening.end():closing.start()]
            headers, separator, _ = raw.partition(b"\r\n\r\n")
            if not separator:
                raise ValueError("invalid multipart field headers")
            parsed = BytesParser(policy=default).parsebytes(headers + b"\r\n\r\n")
            name = parsed.get_param("name", header="content-disposition")
            parts.append((name, parsed.get_filename(), opening.end() + len(headers) + 4,
                          closing.start(), parsed.get_content_charset() or "utf-8"))
        return marker, parts

    @classmethod
    def _urlencoded_parts(cls, body, content_type):
        charset = cls._content_type(content_type).get_content_charset() or "utf-8"
        parts = body.split(b"&")
        if len(parts) > 512:
            raise ValueError("too many form fields to edit")
        names = [(index, unquote_plus(part.partition(b"=")[0].decode("ascii"), encoding=charset, errors="strict"))
                 for index, part in enumerate(parts) if part]
        return parts, names, charset

    @staticmethod
    def _json_parts(body):
        text = body.decode("utf-8")
        decoder = json.JSONDecoder()
        spaces = re.compile(r"[ \t\r\n]*")
        def whitespace(index):
            return spaces.match(text, index).end()
        index = whitespace(1 if text.startswith("\ufeff") else 0)
        if text[index:index + 1] != "{":
            raise ValueError("JSON replay requires an object")
        index, parts = whitespace(index + 1), []
        while text[index:index + 1] != "}":
            name, index = decoder.raw_decode(text, index)
            if not isinstance(name, str):
                raise ValueError("invalid JSON field name")
            index = whitespace(index)
            if text[index:index + 1] != ":":
                raise ValueError("missing JSON field separator")
            start = whitespace(index + 1)
            try:
                _, end = decoder.raw_decode(text, start)
            except RecursionError as exc:
                raise ValueError("JSON nesting exceeds the field editing limit") from exc
            parts.append((name, start, end))
            index = whitespace(end)
            if text[index:index + 1] == "}":
                break
            if text[index:index + 1] != ",":
                raise ValueError("invalid JSON object separator")
            index = whitespace(index + 1)
            if text[index:index + 1] == "}":
                raise ValueError("invalid trailing JSON comma")
        if text[index:index + 1] != "}" or whitespace(index + 1) != len(text):
            raise ValueError("invalid JSON object ending")
        return text, parts

    @classmethod
    def _fields(cls, item):
        body, content_type = item["body"] or b"", item["headers"].get("content-type", "")
        media_type = content_type.lower()
        if "application/x-www-form-urlencoded" in media_type:
            return [(name, "") for _, name in cls._urlencoded_parts(body, content_type)[1]]
        if "multipart/form-data" in media_type:
            return [(part[0], "") for part in cls._multipart_parts(body, content_type)[1] if part[0]]
        if "json" in media_type:
            return [(name, "") for name, _, _ in cls._json_parts(body)[1]]
        raise ValueError("field edits require an observed form or JSON object")

    def captured_requests(self):
        cards, field_names = [], []
        for item in self._requests.values():
            try:
                names = sorted({name for name, _ in self._fields(item)})
            except (ValueError, UnicodeError, LookupError, RecursionError):
                names = None
            url = self._public_url(item["url"])
            card = {"id": item["id"], "url": url, "method": item["method"], "fields": None, "step": item["step"]}
            if len(url) > 160:
                card.update(url=url[:100] + "…" + url[-59:], url_truncated=True)
            if "authorization" in item["headers"]:
                card["credential_headers"] = ["authorization"]
            cards.append(card)
            field_names.append(names)
        for card, names in zip(cards, field_names):
            if names is not None and len(names) <= 24:
                card["fields"] = names
                if len(json.dumps(cards, ensure_ascii=False)) > 2800:
                    card["fields"] = None
        return cards

    async def replay_request(self, request_id, fields=None):
        if not isinstance(request_id, str) or request_id not in self._requests:
            raise ValueError("unknown or expired request id; inspect captured_requests()")
        item = self._requests[request_id]
        self._check(item["url"], item["method"])
        body = item["body"]
        if fields is not None:
            if not isinstance(fields, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in fields.items()):
                raise ValueError("fields must map existing field names to string values")
            pairs = self._fields(item)
            unknown = set(fields) - {name for name, _ in pairs}
            if unknown:
                raise ValueError("unknown field names: " + ", ".join(sorted(unknown)))
            content_type = item["headers"].get("content-type", "")
            media_type = content_type.lower()
            if "application/x-www-form-urlencoded" in media_type:
                parts, names, charset = self._urlencoded_parts(body, content_type)
                for index, name in names:
                    if name in fields:
                        parts[index] = parts[index].partition(b"=")[0] + b"=" + quote_plus(fields[name], encoding=charset).encode("ascii")
                body = b"&".join(parts)
            elif "multipart/form-data" in media_type:
                marker, parts = self._multipart_parts(body, content_type)
                for name, filename, start, end, charset in reversed(parts):
                    if name in fields:
                        value = fields[name].encode(charset)
                        if filename is not None or marker in value:
                            raise ValueError("field edit would change file data or multipart structure")
                        body = body[:start] + value + body[end:]
            else:
                text, parts = self._json_parts(body)
                for name, start, end in reversed(parts):
                    if name in fields:
                        text = text[:start] + json.dumps(fields[name], ensure_ascii=False) + text[end:]
                body = text.encode("utf-8")
        # Cookies come from the live browser context; transport headers must match the replayed bytes.
        headers = {k: v for k, v in item["headers"].items()
                   if k not in {"cookie", "host", "content-length", "connection", "transfer-encoding", "accept-encoding"}
                   and not k.startswith(":")}
        response = await self.context.request.fetch(item["url"], method=item["method"], headers=headers,
                                                    data=body, max_redirects=0)
        return await self._observation(response)

    async def http_get(self, url):
        self._check(url, "GET")
        return await self._observation(await self.context.request.get(url, max_redirects=0))

    @staticmethod
    async def _observation(response):
        raw = await response.body()
        headers = {k.lower(): v for k, v in response.headers.items()}
        for name in {"authorization", "proxy-authorization", "set-cookie"} & headers.keys():
            headers[name] = (re.sub(r"(?m)^([^=;\s]+)=[^;\r\n]*", r"\1=[redacted]", headers[name])
                             if name == "set-cookie" else "[redacted]")
        content_type = headers.get("content-type", "")
        media_type = content_type.lower()
        textual = media_type.startswith("text/") or any(kind in media_type for kind in (
            "json", "javascript", "xml", "svg", "x-www-form-urlencoded"))
        body = ""
        if textual:
            charset = BrowserHTTP._content_type(content_type).get_content_charset() or "utf-8"
            try:
                body = raw.decode(charset, errors="replace")
            except LookupError:
                body = raw.decode("utf-8", errors="replace")
        result = {"url": response.url, "status": response.status, "ok": response.ok, "headers": headers,
                  "content_type": content_type, "length": len(body) if textual else len(raw), "byte_length": len(raw),
                  "body": body[:2000], "body_truncated": len(body) > 2000, "binary": not textual}
        if "json" in media_type:
            try:
                result["json"] = json.loads(body)
            except ValueError as exc:
                result["parse_error"] = str(exc)[:200]
        return result

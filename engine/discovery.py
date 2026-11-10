"""Offline summaries of declared web inputs. No requests or vulnerability verdicts.

This module uses only the standard library so it can be copied into a runtime.
JavaScript analysis is deliberately conservative; unresolved URLs remain hints.
"""

from html.parser import HTMLParser
import re
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit


_MAX_SCAN = 2_000_000
_MAX_ITEMS = 2000
_MAX_EXPRESSION = 4096
_TEMPLATE = chr(96)
_DYNAMIC = "$" + "{"
_ASSET_SUFFIXES = (".css", ".svg", ".png", ".jpg", ".jpeg", ".gif", ".ico",
                   ".woff", ".woff2", ".ttf", ".map", ".webp", ".mp4")
_REQUEST = re.compile(
    r"\b(fetch|axios(?:\.(get|post|put|patch|delete|head|options))?|"
    r"http(?:Client)?\.(get|post|put|patch|delete|head|options))\s*\(",
    re.IGNORECASE,
)
_JS_HEAD = re.compile(r'''^\s*(?:import\s*(?:\{|\(|["'])|export\s*(?:\{|default\b)|
    (?:const|let|var)\s+[A-Za-z_$]|!function\b|\(\s*\(\s*\)\s*=>)''', re.X)


def _http_url(value, base=None):
    if not isinstance(value, str) or not value or len(value) > 2048:
        raise ValueError("An absolute HTTP URL is required")
    if re.search(r"[\x00-\x20\x7f\\]", value):
        raise ValueError("Invalid URL characters")
    parts = urlsplit(urljoin(base, value) if base else value)
    if (parts.scheme not in ("http", "https") or not parts.hostname
            or parts.username is not None or parts.password is not None or parts.port == 0):
        raise ValueError("An HTTP URL without credentials is required")
    host = parts.hostname.lower().encode("idna").decode("ascii")
    if ":" in host:
        host = "[" + host + "]"
    port = parts.port
    if port is not None and port != (443 if parts.scheme == "https" else 80):
        host += ":" + str(port)
    return urlunsplit((parts.scheme, host, parts.path or "/", parts.query, ""))


def _origin(url):
    parts = urlsplit(url)
    return parts.scheme, parts.hostname, parts.port or (443 if parts.scheme == "https" else 80)


def _resolve(value, base, scope):
    try:
        url = _http_url(value, base)
        if _origin(url) == scope:
            return url
    except (ValueError, UnicodeError):
        pass
    return None


def _input_names(url):
    try:
        return sorted({name for name, _ in parse_qsl(urlsplit(url).query, keep_blank_values=True)
                       if name and len(name) <= 256 and "\x00" not in name})
    except ValueError:
        return []


def _blank_url(url, extra=()):
    parts = urlsplit(url)
    names = sorted(set(_input_names(url)) | set(extra))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode([(name, "") for name in names]), ""))


class _Summary:
    def __init__(self, source, scope, limit, truncated=False):
        self.source, self.scope, self.limit = source, scope, limit
        self.assets, self.surfaces, self.hints = {}, {}, {}
        self.truncated = truncated

    def asset(self, raw, base, kind):
        url = _resolve(raw, base, self.scope)
        if url and len(self.assets) < _MAX_ITEMS:
            self.assets.setdefault(url, {"url": url, "kind": kind})
        elif url:
            self.truncated = True

    def hint(self, kind, **values):
        item = {"kind": kind, "source_url": self.source, "method": None, "inputs": [],
                "input_known": False, "declared_only": True, **values}
        item["snippet"] = item.get("snippet", "")[:240]
        if len(item["inputs"]) > self.limit:
            item["inputs"] = item["inputs"][:self.limit]
            self.truncated = True
        if isinstance(item.get("path_hint"), str) and len(item["path_hint"]) > 2048:
            item["path_hint"] = item["path_hint"][:2048]
            self.truncated = True
        key = (kind, item.get("url"), item.get("path_hint"), item.get("method"), tuple(item["inputs"]))
        if len(self.hints) < _MAX_ITEMS:
            self.hints.setdefault(key, item)
        else:
            self.truncated = True

    def surface(self, url, method, inputs):
        names = sorted(set(inputs))
        for name in names or [""]:
            item = {"kind": "surface", "relation": "discovered",
                    "locator": {"url": url, "method": method, "input": name, "auth_context": "unknown"},
                    "hypothesis": None}
            key = (url, method, name)
            if len(self.surfaces) < _MAX_ITEMS:
                self.surfaces.setdefault(key, item)
            else:
                self.truncated = True

    def result(self):
        def asset_priority(item):
            name = urlsplit(item["url"]).path.rsplit("/", 1)[-1].lower()
            support = bool(re.search(r"polyfill|vendor|runtime|framework|chunk", name))
            application = bool(re.search(r"(?:^|[-_.])(?:main|app|entry|index|bundle)(?:[-_.]|$)", name))
            return support, item["kind"] != "module", not application

        surfaces = sorted(self.surfaces.values(), key=lambda item: not bool(item["locator"]["input"]))
        hints = sorted(self.hints.values(), key=lambda item: (
            not item["input_known"], item["kind"] == "url_literal", item["kind"] == "client_route"))
        values = {"assets": sorted(self.assets.values(), key=asset_priority),
                  "surfaces": surfaces, "hints": hints}
        return {**{key: items[:self.limit] for key, items in values.items()},
                "truncated": self.truncated or any(len(items) > self.limit for items in values.values())}


class _HTML(HTMLParser):
    def __init__(self, summary, base):
        super().__init__(convert_charrefs=True)
        self.summary, self.base = summary, base
        self.forms, self.controls = [], []
        self.current = None
        self.base_seen = False
        self.inline, self._script = [], None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "base" and attrs.get("href") and not self.base_seen:
            self.base_seen = True
            try:
                self.base = _http_url(attrs["href"], self.base)
            except (ValueError, UnicodeError):
                pass
        elif tag == "script":
            kind = "module" if attrs.get("type", "").lower() == "module" else "script"
            if attrs.get("src"):
                self.summary.asset(attrs["src"], self.base, kind)
            elif "json" not in attrs.get("type", "").lower():
                self._script = []
        elif tag == "link" and "modulepreload" in attrs.get("rel", "").lower().split() and attrs.get("href"):
            self.summary.asset(attrs["href"], self.base, "module")
        elif tag == "form":
            self.current = len(self.forms)
            self.forms.append({"id": attrs.get("id"), "action": attrs.get("action") or self.summary.source,
                               "method": attrs.get("method", "GET").upper(), "fields": [], "base": self.base})
        elif tag in ("input", "select", "textarea", "button") and attrs.get("name") and "disabled" not in attrs:
            if len(attrs["name"]) <= 256 and "\x00" not in attrs["name"]:
                self.controls.append((attrs.get("form"), self.current, attrs["name"]))
        elif tag == "a" and attrs.get("href"):
            raw = attrs["href"]
            if raw.startswith("#/"):
                self.summary.hint("client_route", path_hint=raw, snippet=raw)
            url = _resolve(raw, self.base, self.summary.scope)
            if url and _input_names(url):
                names = _input_names(url)
                url = _blank_url(url)
                self.summary.surface(url, "GET", names)
                self.summary.hint("request", url=url, method="GET", inputs=names,
                                  input_known=True, snippet=raw)

    def handle_endtag(self, tag):
        if tag == "form":
            self.current = None
        elif tag == "script" and self._script is not None:
            self.inline.append("".join(self._script))
            self._script = None

    def handle_data(self, data):
        if self._script is not None:
            self._script.append(data)

    def finish(self):
        by_id = {form["id"]: index for index, form in enumerate(self.forms) if form["id"]}
        for form_id, current, name in self.controls:
            index = by_id.get(form_id) if form_id is not None else current
            if index is not None:
                self.forms[index]["fields"].append(name)
        for form in self.forms:
            method = form["method"]
            if method not in ("GET", "POST"):
                continue
            url = _resolve(form["action"], form["base"], self.summary.scope)
            if not url:
                continue
            names = sorted(set(form["fields"]) | set(_input_names(url)))
            url = _blank_url(url, form["fields"] if method == "GET" else ())
            self.summary.surface(url, method, names)
            self.summary.hint("form", url=url, method=method, inputs=names,
                              input_known=bool(names), snippet=form["action"])
        for code in self.inline:
            _javascript(code, self.summary, self.base)


def _decode(raw):
    def decode_escape(match):
        value = match.group(1)
        if value.startswith("u") and len(value) == 5:
            return chr(int(value[1:], 16))
        if value.startswith("x") and len(value) == 3:
            return chr(int(value[1:], 16))
        return {"n": "\n", "r": "\r", "t": "\t"}.get(value, value)
    return re.sub(r"\\(u[0-9a-fA-F]{4}|x[0-9a-fA-F]{2}|.)", decode_escape, raw)


def _regex_start(code, index):
    prefix = code[max(0, index - 32):index].rstrip()
    return (not prefix or prefix[-1] in "=(:,![{;?&|+-*%^~<>"
            or bool(re.search(r"\b(?:return|case|throw|yield|await)\s*$", prefix)))


def _regex_end(code, start):
    index, bracket = start + 1, False
    while index < len(code):
        char = code[index]
        if char == "\\":
            index += 2
            continue
        if char in "\r\n":
            return None
        if char == "[":
            bracket = True
        elif char == "]":
            bracket = False
        elif char == "/" and not bracket:
            index += 1
            while index < len(code) and code[index].isalpha():
                index += 1
            return index
        index += 1
    return None


def _quoted_end(code, start, depth=0):
    if depth > 32:
        return len(code)
    quote, index = code[start], start + 1
    while index < len(code):
        if code[index] == "\\":
            index += 2
        elif code[index] == quote:
            return index + 1
        elif quote == _TEMPLATE and code.startswith(_DYNAMIC, index):
            index = _interpolation_end(code, index + 2, depth + 1)
        else:
            index += 1
    return len(code)


def _interpolation_end(code, start, depth):
    index, braces = start, 1
    while index < len(code):
        if code.startswith("//", index):
            end = code.find("\n", index + 2)
            index = len(code) if end < 0 else end
        elif code.startswith("/*", index):
            end = code.find("*/", index + 2)
            index = len(code) if end < 0 else end + 2
        elif code[index] in ("'", '"', _TEMPLATE):
            index = _quoted_end(code, index, depth)
        elif code[index] == "/" and _regex_start(code, index) and (end := _regex_end(code, index)):
            index = end
        elif code[index] == "{":
            braces += 1
            index += 1
        elif code[index] == "}":
            braces -= 1
            index += 1
            if not braces:
                return index
        else:
            index += 1
    return len(code)


def _lex(code):
    strings, masked = [], list(code)
    index = 0
    while index < len(code):
        if code.startswith("//", index):
            end = code.find("\n", index + 2)
            end = len(code) if end < 0 else end
        elif code.startswith("/*", index):
            end = code.find("*/", index + 2)
            end = len(code) if end < 0 else end + 2
        elif code[index] == "/" and _regex_start(code, index) and (end := _regex_end(code, index)):
            pass
        elif code[index] in ("'", '"', _TEMPLATE):
            end = _quoted_end(code, index)
            if end > index + 1 and code[end - 1] == code[index]:
                strings.append({"start": index, "end": end, "value": _decode(code[index + 1:end - 1])})
        else:
            index += 1
            continue
        masked[index:end] = " " * (end - index)
        index = end
    return strings, "".join(masked)


def _arguments(code, masked, start):
    args, depth, current = [], 0, start
    for index in range(start, min(len(code), start + _MAX_EXPRESSION)):
        char = masked[index]
        if char in "([{":
            depth += 1
        elif char in ")]}":
            if depth == 0:
                args.append(code[current:index].strip())
                return args, index + 1, False
            depth -= 1
        elif char == "," and depth == 0:
            args.append(code[current:index].strip())
            current = index + 1
    end = min(len(code), start + _MAX_EXPRESSION)
    args.append(code[current:end].strip())
    return args, end, True


def _properties(value):
    value = value.strip()
    if not value.startswith("{"):
        return {}
    _, masked = _lex(value)
    parts, _, _ = _arguments(value, masked, 1)
    result = {}
    for part in parts:
        match = re.match(r"""^\s*(?:([A-Za-z_$][\w$]*)|["']([^"']+)["'])\s*(?::(.*))?$""", part, re.S)
        if match:
            name = match.group(1) or match.group(2)
            if 0 < len(name) <= 256 and "\x00" not in name:
                result[name] = match.group(3).strip() if match.group(3) is not None else ""
    return result


def _url_expression(expression):
    tokens, _ = _lex(expression)
    if not tokens:
        return None, False, True
    token = tokens[0]
    raw = token["value"]
    prefix = expression[:token["start"]].strip()
    suffix = expression[token["end"]:].strip()
    origin_unknown = bool(prefix)
    if raw.startswith(_DYNAMIC):
        match = re.match(r"^\$\{[^}]+\}(?=/)", raw)
        if match:
            raw, origin_unknown = raw[match.end():], True
    path = raw.split("?", 1)[0]
    dynamic_path = _DYNAMIC in path or (bool(suffix) and "?" not in raw)
    return raw, origin_unknown, dynamic_path


def _request(match, args, summary, base, snippet):
    expression = args[0] if args else ""
    raw, origin_unknown, dynamic_path = _url_expression(expression)
    called = match.group(1).lower()
    method = (match.group(2) or match.group(3) or "GET").upper()
    options = _properties(args[1]) if len(args) > 1 else {}
    if called == "fetch" and "method" in options:
        literal, _ = _lex(options["method"])
        method = literal[0]["value"].upper() if literal and re.fullmatch("[A-Za-z]{1,16}", literal[0]["value"]) else None
    elif called == "fetch" and len(args) > 1 and not args[1].lstrip().startswith("{"):
        method = None
    inputs = set()
    if raw:
        inputs.update(_input_names("https://hint.invalid" + raw if raw.startswith("/") else raw))
    inputs.update(_properties(options.get("params", "")))
    if method in ("POST", "PUT", "PATCH") and called != "fetch" and len(args) > 1:
        inputs.update(_properties(args[1]))
    if called == "fetch":
        body = options.get("body", "")
        object_start = re.search(r"(?:JSON\.stringify|URLSearchParams)\s*\(\s*(\{)", body)
        if object_start:
            inputs.update(_properties(body[object_start.start(1):]))
    names = sorted(inputs)
    if not raw or origin_unknown or dynamic_path:
        probe = _resolve(raw, base, summary.scope) if raw and raw.startswith("/") and not dynamic_path and names else None
        probe_details = ({"probe_url": _blank_url(probe, _properties(options.get("params", ""))
                                              if method == "GET" else ()), "origin_assumed": True}
                         if probe and origin_unknown else {})
        summary.hint("dynamic_request", path_hint=raw, method=method, inputs=names, input_known=bool(names),
                     origin_unknown=origin_unknown, reason="dynamic_path" if dynamic_path else "dynamic_origin",
                     snippet=snippet, **probe_details)
        return
    url = _resolve(raw, base, summary.scope)
    if not url:
        return
    url = _blank_url(url, _properties(options.get("params", "")) if method == "GET" else ())
    summary.hint("request", url=url, method=method, inputs=names, input_known=bool(names),
                 reason=None if names else "input_not_declared", snippet=snippet)
    if method and not urlsplit(url).path.lower().endswith(_ASSET_SUFFIXES):
        summary.surface(url, method, names)


def _javascript(code, summary, base):
    tokens, masked = _lex(code)
    consumed = []
    for index, match in enumerate(_REQUEST.finditer(masked)):
        if index >= _MAX_ITEMS:
            summary.truncated = True
            break
        args, end, truncated = _arguments(code, masked, match.end())
        summary.truncated |= truncated
        consumed.append((match.start(), end))
        _request(match, args, summary, base, code[match.start():min(end, match.start() + 240)])
    for token in tokens:
        raw = token["value"]
        prefix = code[max(0, token["start"] - 100):token["start"]]
        module = re.search(r"\b(?:import\s*(?:\(\s*)?|from\s*)$", prefix)
        if module and _DYNAMIC not in raw and raw.startswith((".", "/", "http://", "https://")):
            summary.asset(raw, base, "module")
            continue
        if any(start <= token["start"] < end for start, end in consumed):
            continue
        if raw.startswith("/") and not raw.startswith("//") and not urlsplit(raw).path.lower().endswith(_ASSET_SUFFIXES):
            url = _resolve(raw, base, summary.scope) if _DYNAMIC not in raw else None
            summary.hint("url_literal", path_hint=raw, url=_blank_url(url) if url else None,
                         inputs=_input_names(url) if url else [], input_known=bool(url and _input_names(url)),
                         origin_assumed=True, reason="not_bound_to_http_call", snippet=code[token["start"]:token["end"]])


def analyze_response(body, source_url, *, content_type="", allowed_origin=None, limit=20):
    """Return declarations and resource hints, without fetching or confirming them."""
    if not isinstance(body, str) or type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("Expected text and a limit from 1 to 100")
    source = _http_url(source_url)
    scope = _origin(_http_url(allowed_origin)) if allowed_origin is not None else _origin(source)
    if _origin(source) != scope:
        raise ValueError("Source URL is outside the allowed origin")
    summary = _Summary(source, scope, limit, len(body) > _MAX_SCAN)
    segments = [body] if len(body) <= _MAX_SCAN else [body[:_MAX_SCAN * 3 // 4], body[-_MAX_SCAN // 4:]]
    content_type = content_type.lower()
    is_html = "html" in content_type or bool(re.search(r"<(?:!doctype|html|head|body|form|script)\b", body[:4096], re.I))
    is_js = ("javascript" in content_type or urlsplit(source).path.lower().endswith((".js", ".mjs"))
             or (not is_html and bool(_JS_HEAD.search(body[:4096]))))
    base = source
    for segment in segments:
        if is_html:
            parser = _HTML(summary, base)
            parser.feed(segment)
            parser.close()
            parser.finish()
            base = parser.base
        elif is_js:
            _javascript(segment, summary, base)
    return summary.result()

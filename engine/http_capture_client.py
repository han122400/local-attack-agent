"""Client for the runtime's attempt-bound HTTP recorder."""

import base64
import asyncio
import json
import os
from urllib.request import ProxyHandler, Request, build_opener


def request(url, method="GET", headers=None, body=b"", *, params=None, endpoint=None):
    endpoint = endpoint or os.environ.get("HARNESS_HTTP_ENDPOINT")
    if not endpoint:
        raise RuntimeError("HTTP capture unavailable: run_python에서는 주입된 audit_http를 사용하세요.")
    if isinstance(body, str):
        body = body.encode("utf-8")
    payload = {"url": url, "method": method, "headers": headers or {},
               "body": base64.b64encode(body).decode("ascii")}
    if params is not None:
        payload["params"] = params
    message = Request(endpoint, data=json.dumps(payload).encode("utf-8"),
                      headers={"Content-Type": "application/json"})
    with build_opener(ProxyHandler({})).open(message, timeout=35) as response:
        result = json.load(response)
    result["body_bytes"] = base64.b64decode(result.pop("body_base64"), validate=True)
    result["body"] = result["body_bytes"].decode("utf-8", errors="replace")
    try:
        result["json"] = json.loads(result["body_bytes"])
    except (ValueError, UnicodeError):
        result["json"] = None
    return result


def async_helpers(endpoint):
    async def audit_http(url, method="GET", headers=None, body=b"", *, params=None):
        return await asyncio.to_thread(request, url, method, headers, body, params=params, endpoint=endpoint)

    async def get_text(url):
        return (await audit_http(url))["body"]

    async def get_json(url):
        response = await audit_http(url)
        return json.loads(response["body_bytes"])

    async def http_get(url):
        response = await audit_http(url)
        result = {key: response[key] for key in ("request_id", "url", "status", "ok", "content_type")}
        result.update(length=len(response["body"]), body=response["body"][:2000])
        if "json" in response["content_type"]:
            try:
                result["json"] = json.loads(response["body_bytes"])
            except (ValueError, UnicodeError) as exc:
                result["parse_error"] = str(exc)[:200]
        return result

    return {"audit_http": audit_http, "http_get": http_get, "get_text": get_text, "get_json": get_json}

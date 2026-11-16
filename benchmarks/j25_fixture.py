"""Isolated black-box fixture. The server never serves its oracle or manifest."""

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


MANIFEST = json.loads((Path(__file__).parent / "j25_fixture_manifest.json").read_text())


def canary(case_id):
    return f"J25-FIXTURE-PRIVATE-{case_id}-8d7f42c1"


def make_server(split):
    selected = {item["surface"]: item for item in MANIFEST["cases"] if item["split"] == split}
    if not selected:
        raise ValueError("unknown fixture split")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            parts = urlsplit(self.path)
            if parts.path == "/":
                links = "".join(f'<li><a href="{path}">Record {i + 1}</a></li>'
                                for i, path in enumerate(selected))
                links += "".join(f'<li><a href="{item["surface"]}">Control {i + 1}</a></li>'
                                 for i, item in enumerate(MANIFEST["controls"]))
                self.respond(f"<html><body><h1>Records</h1><ul>{links}</ul></body></html>")
                return
            case = selected.get(parts.path)
            if case is None:
                if any(item["surface"] == parts.path for item in MANIFEST["controls"]):
                    self.respond("<html><body>Public control record</body></html>")
                else:
                    self.respond("missing", 404)
                return
            label = case["category"]
            secret = canary(case["id"])
            field = case["field"]
            query = parse_qs(parts.query).get(field, [""])[0]
            owner = "sid=fixture-owner" in self.headers.get("Cookie", "")
            if label == "authorization":
                body = f"<html><body><h1>Account record</h1><div>{secret}</div></body></html>"
            elif label == "browser_input":
                visible = secret if owner else "public"
                body = ('<html><body><h1>Message preview</h1><div id="secret">' + visible +
                    f'</div><input id="{field}"><div id="out"></div><script>document.getElementById("{field}")'
                    '.addEventListener("input",e=>document.getElementById("out").innerHTML=e.target.value)'
                    '</script></body></html>')
            elif label == "injection":
                exposed = bool(re.search(r"(?i)(union\s+select|or\s+['\"]?1['\"]?\s*=\s*['\"]?1)", query))
                body = (f'<html><body><h1>Search record</h1><form action="{parts.path}" method="get">'
                        f'<input name="{field}"></form><div>' + (secret if exposed else "No results") + '</div></body></html>')
            else:
                exposed = "../" in query or "%2e%2e" in parts.query.lower()
                body = (f'<html><body><h1>File preview</h1><form action="{parts.path}" method="get">'
                        f'<input name="{field}"></form><div>' + (secret if exposed else "Public file") + '</div></body></html>')
            self.respond(body)

        def respond(self, body, status=200):
            data = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *_):
            pass

    return ThreadingHTTPServer(("127.0.0.1", 0), Handler)


def test_scope(server, split):
    origin = f"http://127.0.0.1:{server.server_port}/"
    return {"isolation": "recoverable", "allowed_methods": ["POST"], "allowed_paths": ["/unused"],
            "identities": [{"name": "owner", "cookies": [
                {"name": "sid", "value": "fixture-owner", "url": origin}]}],
            "proof_canaries": [{"label": item["information_label"], "value": canary(item["id"]),
                "protected": True, "authorized_identities": ["owner"]}
                for item in MANIFEST["cases"] if item["split"] == split]}


def oracle(split):
    return {"status": "available", "truth_ids": [item["oracle_id"] for item in MANIFEST["cases"]
            if item["split"] == split and item["eligible"]]}

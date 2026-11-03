"""Versioned, pure boundary contracts for model-authored v4 discoveries/results.

Validation preserves a model's claim; it never issues a verification verdict.
"""

from __future__ import annotations

import hashlib
import json
import re
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from session_log import safe_relative_path
from risk_rubric import observations, OBSERVATIONS_SCHEMA


SCHEMA_VERSION = 1
MAX_RESULT_BYTES = 1024 * 1024
MAX_EVIDENCE_BYTES = 10 * 1024 * 1024
MAX_TOTAL_EVIDENCE_BYTES = 50 * 1024 * 1024
NODE_ID = re.compile(r"N[0-9]{4,}\Z")
ATTEMPT_ID = re.compile(r"A[0-9]{4,}\Z")
RELATIONS = ("discovered", "refines", "enabled_by")
VERDICTS = ("confirmed", "rejected", "inconclusive")


class ContractError(ValueError):
    """Untrusted input did not satisfy the versioned v4 contract."""


def _object(value, required, optional=()) -> dict:
    if not isinstance(value, dict) or not set(required) <= value.keys() or value.keys() - set(required) - set(optional):
        raise ContractError("Object has missing or unsupported fields")
    return value


def _text(value, label: str, limit: int = 2000, *, empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > limit or "\x00" in value or (not empty and not value.strip()):
        raise ContractError(f"Invalid {label}")
    return value


def validate_id(value: str, *, attempt: bool = False) -> str:
    pattern = ATTEMPT_ID if attempt else NODE_ID
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ContractError("Invalid attempt/node ID")
    return value


def normalize_url(value: str) -> str:
    _text(value, "URL", 2048)
    if re.search(r"[\x00-\x20\x7f\\]", value):
        raise ContractError("Invalid URL characters")
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username is not None or parsed.password is not None:
            raise ValueError("Expected HTTP(S) URL without credentials")
        port = parsed.port
        if port == 0:
            raise ValueError("Invalid port")
        host = parsed.hostname.lower()
        host = f"[{host}]" if ":" in host else host.encode("idna").decode("ascii")
        if port is not None and port != {"http": 80, "https": 443}[parsed.scheme]:
            host += f":{port}"
        return urlunsplit((parsed.scheme, host, parsed.path or "/", parsed.query, ""))
    except (ValueError, UnicodeError) as exc:
        raise ContractError("Invalid HTTP(S) URL") from exc


def validate_locator(value: dict) -> dict:
    _object(value, ("url", "method", "input", "auth_context"), ("route_template",))
    method = _text(value["method"], "method", 16).upper()
    if not re.fullmatch(r"[A-Z]+", method):
        raise ContractError("Invalid HTTP method")
    result = {"url": normalize_url(value["url"]), "method": method,
              "input": _text(value["input"], "input name", 256, empty=True),
              "auth_context": _text(value["auth_context"], "auth context", 256)}
    if "route_template" in value:
        template = _text(value["route_template"], "route template", 2048)
        if not template.startswith("/") or template.startswith("//") or any(c in template for c in "?#\\"):
            raise ContractError("Invalid route template")
        result["route_template"] = template
    return result


def validate_discovery(value: dict) -> dict:
    _object(value, ("kind", "relation", "locator", "hypothesis"), ("observations",))
    if value["kind"] not in ("surface", "candidate") or value["relation"] not in RELATIONS:
        raise ContractError("Invalid discovered node kind/relation")
    hypothesis = value["hypothesis"]
    if value["kind"] == "candidate":
        _object(hypothesis, ("type", "statement"))
        category = _text(hypothesis["type"], "hypothesis type", 64).lower()
        if not re.fullmatch(r"[a-z][a-z0-9_-]*", category):
            raise ContractError("Invalid hypothesis type")
        hypothesis = {"type": category, "statement": _text(hypothesis["statement"], "hypothesis", 4000)}
    elif hypothesis is not None:
        raise ContractError("Surface nodes have no vulnerability hypothesis")
    locator = validate_locator(value["locator"])
    if value["kind"] == "surface":
        parts = urlsplit(locator["url"])
        asset_suffixes = (".js", ".mjs", ".cjs", ".css", ".map", ".svg", ".png", ".jpg",
                          ".jpeg", ".gif", ".ico", ".woff", ".woff2", ".ttf", ".webp")
        input_name = locator["input"].lower()
        query_names = {name.lower() for name, _ in parse_qsl(parts.query, keep_blank_values=True)}
        asset_input = input_name.endswith(asset_suffixes)
        asset_path = parts.path.lower().endswith(asset_suffixes)
        if ((asset_input and (parts.path in ("", "/") or parts.path.rsplit("/", 1)[-1].lower() == input_name))
                or (asset_path and input_name not in query_names)):
            raise ContractError("Static resource filename is not an input surface")
    result = {"kind": value["kind"], "relation": value["relation"],
              "locator": locator, "hypothesis": hypothesis}
    if "observations" in value:
        result["observations"] = observations(value["observations"])
    return result


def dedup_key(discovery: dict) -> str:
    locator = discovery["locator"]
    url = locator["url"]
    if "route_template" in locator:
        parts = urlsplit(url)
        url = urlunsplit((parts.scheme, parts.netloc, locator["route_template"], "", ""))
    identity = [discovery["kind"], url, locator["method"], locator["input"], locator["auth_context"],
                (discovery["hypothesis"] or {}).get("type")]
    return hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode("utf-8")).hexdigest()


def evidence_path(value: str) -> str:
    _text(value, "evidence path", 2048)
    try:
        relative = safe_relative_path(value)
    except ValueError as exc:
        raise ContractError(str(exc)) from exc
    if len(relative.parts) < 2 or relative.parts[0] not in ("work", "tools"):
        raise ContractError("Evidence must be under work/ or tools/")
    if len(relative.parts) >= 3 and relative.parts[:2] == ("work", "node-results"):
        raise ContractError("Result JSON cannot be used as independent evidence")
    for part in relative.parts:
        if part.rstrip(" .") != part or re.fullmatch(r"(?i:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", part):
            raise ContractError("Invalid evidence filename")
    return relative.as_posix()


def validate_result(value: dict, *, attempt_id: str, node_id: str) -> dict:
    fields = ("schema_version", "attempt_id", "node_id", "discovered", "proposed_verdict",
              "claim", "evidence_refs", "next_observation")
    _object(value, fields, ("observations",))
    if type(value["schema_version"]) is not int or value["schema_version"] != SCHEMA_VERSION:
        raise ContractError("Unsupported result schema version")
    if validate_id(value["attempt_id"], attempt=True) != attempt_id or validate_id(value["node_id"]) != node_id:
        raise ContractError("Result belongs to a different attempt or node")
    discoveries, evidence = value["discovered"], value["evidence_refs"]
    if not isinstance(discoveries, list) or len(discoveries) > 100 or not isinstance(evidence, list) or len(evidence) > 50:
        raise ContractError("Invalid discovery/evidence list")
    if value["proposed_verdict"] not in VERDICTS:
        raise ContractError("Invalid proposed verdict")
    refs = []
    for entry in evidence:
        _object(entry, ("path", "description"))
        refs.append({"path": evidence_path(entry["path"]),
                     "description": _text(entry["description"], "evidence description", 2000, empty=True)})
    normalized = {"schema_version": SCHEMA_VERSION, "attempt_id": attempt_id, "node_id": node_id,
                  "discovered": [validate_discovery(item) for item in discoveries],
                  "proposed_verdict": value["proposed_verdict"],
                  "claim": _text(value["claim"], "claim", 8000, empty=True), "evidence_refs": refs,
                  "next_observation": _text(value["next_observation"], "next observation", 4000, empty=True)}
    if "observations" in value:
        normalized["observations"] = observations(value["observations"])
    if len(json.dumps(normalized, ensure_ascii=False).encode("utf-8")) > MAX_RESULT_BYTES:
        raise ContractError("Result is too large")
    return normalized


# The same shape can be supplied to a future execution prompt without exposing
# harness-owned node IDs, risk scores or verification decisions in discoveries.
_TEXT = {"type": "string"}
_LOCATOR = {"type": "object", "additionalProperties": False,
            "required": ["url", "method", "input", "auth_context"],
            "properties": {key: _TEXT for key in ("url", "method", "input", "auth_context", "route_template")}}
_HYPOTHESIS = {"type": "object", "additionalProperties": False, "required": ["type", "statement"],
               "properties": {"type": _TEXT, "statement": _TEXT}}
DISCOVERY_SCHEMA = {"type": "object", "additionalProperties": False,
                    "required": ["kind", "relation", "locator", "hypothesis"],
                    "properties": {"kind": {"enum": ["surface", "candidate"]},
                                   "relation": {"enum": list(RELATIONS)}, "locator": _LOCATOR,
                                   "hypothesis": {"anyOf": [_HYPOTHESIS, {"type": "null"}]}, "observations": OBSERVATIONS_SCHEMA},
                    "oneOf": [{"properties": {"kind": {"const": "surface"}, "hypothesis": {"type": "null"}}},
                              {"properties": {"kind": {"const": "candidate"}, "hypothesis": _HYPOTHESIS}}]}
RESULT_SCHEMA = {"type": "object", "additionalProperties": False,
                 "required": ["schema_version", "attempt_id", "node_id", "discovered", "proposed_verdict",
                              "claim", "evidence_refs", "next_observation"],
                 "properties": {"schema_version": {"type": "integer", "const": SCHEMA_VERSION},
                                "attempt_id": {"type": "string", "pattern": "^A[0-9]{4,}$"},
                                "node_id": {"type": "string", "pattern": "^N[0-9]{4,}$"},
                                "discovered": {"type": "array", "maxItems": 100, "items": DISCOVERY_SCHEMA},
                                "proposed_verdict": {"enum": list(VERDICTS)}, "claim": _TEXT,
                                "next_observation": _TEXT, "observations": OBSERVATIONS_SCHEMA,
                                "evidence_refs": {"type": "array", "maxItems": 50, "items": {
                                    "type": "object", "additionalProperties": False, "required": ["path", "description"],
                                    "properties": {"path": _TEXT, "description": _TEXT}}}}}

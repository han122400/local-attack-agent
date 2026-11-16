"""Bounded decisions for the fixed J2.5 investigation actions."""

import copy
import json
from dataclasses import dataclass


@dataclass(frozen=True)
class J25Decision:
    action: str
    reason: str
    ref: str = ""
    hypothesis: str = ""
    normal: str = ""
    test: str = ""
    value: str = ""
    kind: str = ""
    goal_status: str = ""
    call_id: str = ""

    @property
    def plan(self):
        return self.action


def _branch(action, fields, reason_limit=800):
    properties = {"action": {"type": "string", "const": action},
                  "reason": {"type": "string", "minLength": 1, "maxLength": reason_limit}}
    properties.update({name: {"type": "string", "minLength": 1, "maxLength": limit}
                       for name, limit in fields.items()})
    return {"type": "object", "properties": properties, "required": list(properties),
            "additionalProperties": False}


J25_REPORT_SCHEMA = _branch("finish", {}, 8000)
J25_REPORT_SCHEMA["properties"]["goal_status"] = {"type": "string", "enum": ["completed", "partial", "unknown"]}
J25_REPORT_SCHEMA["required"].append("goal_status")
_J25_FIELDS = {"ref": 80, "hypothesis": 500, "normal": 500, "test": 500,
               "value": 500, "kind": 40}
J25_SCHEMA = {"type": "object", "additionalProperties": False,
    "properties": {"action": {"type": "string", "enum": ["observe", "analyze", "compare", "verify", "verify_browser", "verify_access", "interact", "submit", "replay", "identity", "finish"]},
                   "reason": {"type": "string", "maxLength": 800},
                   "goal_status": {"type": "string", "enum": ["", "completed", "partial", "unknown"]},
                   **{name: {"type": "string", "maxLength": limit} for name, limit in _J25_FIELDS.items()}},
    "required": ["action", "reason", "goal_status", *_J25_FIELDS], "oneOf": [
    _branch("observe", {"ref": 80}), _branch("analyze", {"ref": 80}),
    _branch("compare", {"ref": 80, "hypothesis": 500, "normal": 500, "test": 500}),
    _branch("verify", {"ref": 80, "hypothesis": 500, "normal": 500, "test": 500, "kind": 40}),
    _branch("verify_browser", {"ref": 80}),
    _branch("verify_access", {"ref": 80, "value": 80}),
    _branch("interact", {"ref": 80, "value": 500}),
    _branch("submit", {"ref": 80, "value": 500}),
    _branch("replay", {"ref": 80, "value": 500}),
    _branch("identity", {"value": 80}),
    _branch("finish", {}),
]}
for _item in J25_SCHEMA["oneOf"]:
    for _name in _J25_FIELDS:
        if _name not in _item["properties"]:
            _item["properties"][_name] = {"type": "string", "const": ""}
            _item["required"].append(_name)
    _item["properties"]["goal_status"] = (J25_REPORT_SCHEMA["properties"]["goal_status"]
        if _item["properties"]["action"]["const"] == "finish" else {"type": "string", "const": ""})
    _item["required"].append("goal_status")
    if _item["properties"]["action"]["const"] == "verify":
        _item["properties"]["kind"]["enum"] = ["injection", "disclosure"]


_REFERENCE_KIND = {"observe": "pages", "analyze": "assets", "compare": "inputs", "verify": "inputs",
                   "verify_browser": "inputs",
                   "verify_access": "pages",
                   "interact": "inputs", "submit": "inputs", "replay": "inputs"}


def _selectable(item, action):
    return (item.get("interactable", False) if action in {"interact", "submit", "verify_browser"} else
            item.get("replayable", False) if action == "replay" else item.get("supported", True))


def j25_schema_for_context(context: dict, *, report_only: bool) -> dict:
    if report_only:
        return copy.deepcopy(J25_REPORT_SCHEMA)
    schema = copy.deepcopy(J25_SCHEMA)
    refs = context.get("references") or {}
    focus = context.get("focus") or {}
    branches = []
    for branch in schema["oneOf"]:
        action = branch["properties"]["action"]["const"]
        if action == "finish":
            branches.append(branch)
            continue
        if focus and action != focus.get("action"):
            continue
        if action == "identity":
            names = [name for name in context.get("identities", [])
                     if isinstance(name, str) and name != context.get("current_identity")]
            if names:
                branch["properties"]["value"]["enum"] = names
                branches.append(branch)
            continue
        if action == "verify_access":
            names = [name for name in context.get("identities", [])
                     if isinstance(name, str) and name != context.get("current_identity")]
            if not names:
                continue
            branch["properties"]["value"]["enum"] = names
        if action == "submit" and not context.get("mutation_allowed"):
            continue
        ids = [item["id"] for item in refs.get(_REFERENCE_KIND[action], [])
               if isinstance(item, dict) and isinstance(item.get("id"), str)
               and _selectable(item, action)
               and (not focus or item["id"] in focus.get("refs", []))]
        if ids:
            branch["properties"]["ref"]["enum"] = ids
            branches.append(branch)
    schema["oneOf"] = branches
    schema["properties"]["action"]["enum"] = [branch["properties"]["action"]["const"] for branch in branches]
    return schema


def validate_j25_decision(value, *, report_only=False, references=None,
                          identities=None, current_identity=None, mutation_allowed=False):
    if not isinstance(value, dict):
        raise ValueError("Decision must be an object")
    value = dict(value)
    action = value.get("action")
    value.setdefault("goal_status", "unknown" if action == "finish" else "")
    branches = [J25_REPORT_SCHEMA] if report_only else J25_SCHEMA["oneOf"]
    branch = next((item for item in branches if item["properties"]["action"]["const"] == action), None)
    if branch is None or set(value) - set(branch["required"]):
        raise ValueError("Use one allowed action with exactly its required fields")
    for name, rule in branch["properties"].items():
        item = value.get(name, "")
        if rule.get("const") == "" and item == "":
            continue
        if not isinstance(item, str) or not item.strip() or len(item) > rule.get("maxLength", 100):
            raise ValueError(f"Invalid {name}")
        if "const" in rule and item != rule["const"]:
            raise ValueError(f"Invalid {name}")
        if "enum" in rule and item not in rule["enum"]:
            raise ValueError(f"Invalid {name}")
    if action in {"compare", "verify"} and value["normal"] == value["test"]:
        raise ValueError("Normal and test values must differ")
    if references is not None and action in _REFERENCE_KIND:
        if not any(isinstance(item, dict) and item.get("id") == value["ref"]
                   and _selectable(item, action)
                   for item in references.get(_REFERENCE_KIND[action], [])):
            raise ValueError("Reference is not available for this action in the visible context")
    if action in {"identity", "verify_access"} and (identities is None or value["value"] not in identities
                                 or value["value"] == current_identity):
        raise ValueError("Select a different authorized test identity")
    if action == "submit" and not mutation_allowed:
        raise ValueError("Submitting a form requires explicit mutation test scope")
    return J25Decision(**value)


def pack_j25_context(context, budget):
    result = copy.deepcopy(context)
    refs = result.get("references", {})
    def size():
        return len(json.dumps(result, ensure_ascii=False))
    def trim_refs(floor):
        while size() > budget:
            lists = [(key, value) for key, value in refs.items() if isinstance(value, list) and len(value) > floor]
            if not lists:
                break
            key, items = max(lists, key=lambda pair: len(pair[1]))
            removed = items.pop(-1 if result.get("reference_order") == "priority" else 0)
            if isinstance(removed, dict) and isinstance(result.get("pending"), list):
                result["pending"] = [ref for ref in result["pending"] if ref != removed.get("id")]
            result.setdefault("context_omitted", {})[key] = result.get("context_omitted", {}).get(key, 0) + 1
            result["truncated"] = True
    trim_refs(1)
    for key in ("history", "recent", "pending"):
        while size() > budget and result.get(key):
            result[key].pop(0)
            result["truncated"] = True
    trim_refs(0)
    if isinstance(result.get("pending"), list):
        visible = {item.get("id") for items in refs.values() if isinstance(items, list)
                   for item in items if isinstance(item, dict)}
        result["pending"] = [ref for ref in result["pending"] if ref in visible]
    if size() > budget:
        raise ValueError("J2.5 goal and controls exceed the context budget")
    return result

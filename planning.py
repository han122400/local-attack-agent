"""Pure contract for model-authored local investigation commands."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path


ACTIONS = ("read_file", "write_file", "run_command", "run_python", "finish")
_INPUT_FIELDS = ("path", "content", "command", "code")


def _command_branch(action: str, nonempty: tuple[str, ...], allow_empty: tuple[str, ...] = ()) -> dict:
    """One per-action schema so the model cannot leave the required input empty or fill the
    wrong field: unused payload fields are constrained to the empty string by the grammar."""
    properties = {"action": {"type": "string", "const": action},
                  "plan": {"type": "string", "minLength": 1},
                  "reason": {"type": "string", "minLength": 1}}
    for field in _INPUT_FIELDS:
        if field in nonempty:
            properties[field] = {"type": "string", "minLength": 1}
        elif field in allow_empty:
            properties[field] = {"type": "string"}
        else:
            properties[field] = {"type": "string", "const": ""}
    return {"type": "object", "additionalProperties": False, "properties": properties,
            "required": ["action", "plan", "reason", *_INPUT_FIELDS]}


COMMAND_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "action": {"type": "string", "enum": list(ACTIONS)},
        "plan": {"type": "string", "minLength": 1}, "reason": {"type": "string", "minLength": 1},
        "path": {"type": "string"}, "content": {"type": "string"},
        "command": {"type": "string"}, "code": {"type": "string"},
    },
    "required": ["action", "plan", "reason", "path", "content", "command", "code"],
    "oneOf": [
        _command_branch("read_file", ("path",)),
        _command_branch("write_file", ("path",), allow_empty=("content",)),
        _command_branch("run_command", ("command",)),
        _command_branch("run_python", ("code",)),
        _command_branch("finish", ()),
    ],
}

SYSTEM_PROMPT_VERSION = "v3"
SYSTEM_PROMPT = (Path(__file__).resolve().parent / "prompts" / "system"
                 / f"{SYSTEM_PROMPT_VERSION}.txt").read_text(encoding="utf-8")
SYSTEM_PROMPT_SHA256 = sha256(SYSTEM_PROMPT.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Command:
    action: str
    plan: str
    reason: str
    path: str = ""
    content: str = ""
    command: str = ""
    code: str = ""
    call_id: str = ""

    def runtime_payload(self) -> dict:
        return {key: getattr(self, key) for key in ("action", "path", "content", "command", "code")}


def normalize_command_inputs(answer: object) -> tuple[object, list[str]]:
    """Remove only unambiguous payload noise before applying the strict contract.

    Never choose between conflicting payloads or move text into a required field.
    Paths retain their exact semantics and are not repaired here.
    """
    if not isinstance(answer, dict):
        return answer, []
    normalized = dict(answer)
    active = {"write_file": "content", "run_command": "command", "run_python": "code"}.get(answer.get("action"))
    chosen = answer.get(active) if active else None
    fields = []
    for key in ("content", "command", "code"):
        value = answer.get(key)
        if key == active or not isinstance(value, str) or not value:
            continue
        if not value.strip() or (isinstance(chosen, str) and chosen and value == chosen):
            normalized[key] = ""
            fields.append(key)
    return normalized, fields


def validate_command(answer: object) -> Command:
    if not isinstance(answer, dict):
        raise ValueError("Command must be a JSON object")
    if set(answer) - {"action", "plan", "reason", "path", "content", "command", "code"}:
        raise ValueError("Unknown Command fields")
    values = {key: answer.get(key, "") for key in ("action", "plan", "reason", "path", "content", "command", "code")}
    if any(not isinstance(value, str) for value in values.values()):
        raise ValueError("Command fields must be strings")
    action = values["action"]
    if action not in ACTIONS:
        raise ValueError("Unsupported Command action")
    if not values["plan"].strip() or not values["reason"].strip():
        raise ValueError("Command requires a plan and reason")
    needed = {"read_file": ("path",), "write_file": ("path", "content"),
              "run_command": ("command",), "run_python": ("code",), "finish": ()}[action]
    if any(not values[key].strip() for key in needed if key != "content"):
        raise ValueError(f"{action} is missing its input")
    conflicting = [key for key in ("path", "content", "command", "code") if key not in needed and values[key]]
    if conflicting:
        raise ValueError(f"{action} requires unused fields to be empty: {', '.join(conflicting)}")
    if any(len(values[key]) > 2_000_000 for key in ("content", "code", "command")):
        raise ValueError("Command input is too large")
    return Command(**values)

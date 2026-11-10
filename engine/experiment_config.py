"""Syntax validation for experiment mode and independently pinned scorer settings.

Preparation does not assert model availability or adapter compatibility (A3).
No scorer tuning defaults are invented before the first-execution gate.
"""

from __future__ import annotations

import math
import re
from urllib.parse import urlsplit

from engine.contracts import normalize_url


_INTEGER_LIMITS = {"max_retries": (0, 5), "max_calls": (1, 500), "max_calls_per_parent": (1, 500),
                   "batch_size": (1, 100), "max_input_bytes": (1, 1024 * 1024)}
_LOCAL_INTEGERS = {"num_ctx": (256, 1048576), "num_predict": (1, 131072)}


class V4ExecutionUnavailable(RuntimeError):
    """The selected provider requires the later B adapter stage."""


def require_execution_ready(config: dict) -> None:
    if (config["experiment_mode"] == "v4" and config["scorer_provider"] == "local"
            and config["scorer_settings"].get("protocol", "ollama") != "ollama"):
        raise V4ExecutionUnavailable("Only Ollama local scoring is currently supported")


def compatible_prompt(mode: str, requested=None) -> str:
    from engine.planning import SYSTEM_PROMPT_VERSION, available_prompt_versions
    selected = requested or ("v4" if mode == "v4" else SYSTEM_PROMPT_VERSION)
    if selected not in available_prompt_versions() or ((selected == "v4") != (mode == "v4")):
        raise ValueError("Prompt version is incompatible with experiment_mode")
    return selected


def validate_experiment_config(body: dict) -> dict:
    if not isinstance(body, dict):
        raise ValueError("Experiment settings must be an object")
    mode = body.get("experiment_mode", "v3")
    if mode not in ("v3", "v4"):
        raise ValueError("experiment_mode must be v3 or v4")
    if mode == "v3":
        return {"experiment_mode": "v3", "scorer_provider": None, "scorer_model": None, "scorer_settings": {}}
    provider, model = body.get("scorer_provider"), body.get("scorer_model")
    if provider not in ("openrouter", "local"):
        raise ValueError("v4 requires scorer_provider=openrouter or local")
    if not isinstance(model, str) or not model or len(model) > 256 or re.search(r"[\s\x00-\x1f]", model):
        raise ValueError("v4 requires a scorer model ID")
    if provider == "openrouter" and (not re.fullmatch(r"[\w.-]+/[\w.:-]+", model)
                                      or "latest" in model.lower()):
        raise ValueError("OpenRouter scorer requires an explicit versioned model ID")
    raw = body.get("scorer_settings", {})
    if not isinstance(raw, dict):
        raise ValueError("scorer_settings must be an object")
    allowed = {*_INTEGER_LIMITS, "timeout_seconds", "rubric_version", "reevaluation_policy", "max_cost_usd", "time_budget_seconds"}
    if provider == "local":
        allowed |= {*_LOCAL_INTEGERS, "base_url", "protocol", "temperature"}
    if raw.keys() - allowed:
        raise ValueError("Unsupported scorer setting (credentials and execution model settings are not accepted)")
    settings = dict(raw)
    for key, (minimum, maximum) in {**_INTEGER_LIMITS, **_LOCAL_INTEGERS}.items():
        if key in raw and (type(raw[key]) is not int or not minimum <= raw[key] <= maximum):
            raise ValueError(f"Invalid scorer {key}")
    for key, minimum, maximum in (("timeout_seconds", 0.001, 300), ("temperature", 0, 2),
                                  ("max_cost_usd", 0.000001, 100), ("time_budget_seconds", 0.001, 3600)):
        if key in raw and (type(raw[key]) not in (int, float) or not math.isfinite(raw[key]) or not minimum <= raw[key] <= maximum):
            raise ValueError(f"Invalid scorer {key}")
    if "rubric_version" in raw and (not isinstance(raw["rubric_version"], str)
                                   or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", raw["rubric_version"])):
        raise ValueError("Invalid rubric version")
    if "reevaluation_policy" in raw and raw["reevaluation_policy"] != "scoring_fields_v1":
        raise ValueError("Unknown reevaluation policy")
    if "protocol" in raw and raw["protocol"] != "ollama":
        raise ValueError("Unknown local scorer protocol")
    if "base_url" in raw:
        if not isinstance(raw["base_url"], str):
            raise ValueError("Local scorer URL must be a string")
        parsed = urlsplit(raw["base_url"])
        if (parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost", "::1")
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or parsed.path not in ("", "/")):
            raise ValueError("Local scorer URL must be loopback HTTP at the API root")
        settings["base_url"] = normalize_url(raw["base_url"])
    if "max_calls" in raw and raw.get("max_calls_per_parent", 0) > raw["max_calls"]:
        raise ValueError("Parent scorer budget exceeds total scorer budget")
    return {"experiment_mode": mode, "scorer_provider": provider, "scorer_model": model, "scorer_settings": settings}

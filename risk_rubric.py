"""Versioned, categorical scorer input and Decisions Score contract.

No URL, response body, credential, evidence path, or model-authored prose is
permitted in the remote input. These are priority scores, never verdicts.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from urllib.parse import unquote, urlsplit


RUBRIC_VERSION = "risk-priority-v1"
PROJECTION_VERSION = "scoring_fields_v1"
CONTRACT_VERSION = "decisions-score-v2-rounded"
ROUNDING_HALF_UNIT = 0.005
# Gross bounds are derived from five independently rounded probabilities and
# the rounded score. The tighter shared-distribution test below also applies.
PROBABILITY_SUM_TOLERANCE = 5 * ROUNDING_HALF_UNIT + 0.000001
WEIGHTED_SCORE_TOLERANCE = 11 * ROUNDING_HALF_UNIT + 0.000001
# Ascending risk. No fabricated independent impact/likelihood percentages:
# these anchors jointly consider potential impact and observed feasibility.
CRITERIA = (
    "Negligible priority: no observable attacker control or security-relevant behavior; only a generic surface is known.",
    "Low priority: controllable input or a security-relevant route exists, but no observed boundary failure or meaningful impact is supported.",
    "Moderate priority: a concrete anomaly or exposed behavior warrants validation, but impact, access boundary, or reproducibility remains unclear.",
    "High priority: baseline comparison and repeatable observations support a plausible access-boundary failure or exposure of private data with meaningful impact.",
    "Critical priority: repeatable, controlled observations strongly support a severe authorization, sensitive-data, or broad integrity boundary failure; prioritize independent verification immediately.",
)

_ENUMS = {
    "input_control": {"unknown", "none", "partial", "full"},
    "object_scope": {"unknown", "self", "cross_user", "public"},
    "reflection": {"unknown", "none", "escaped", "unescaped"},
    "data_exposure": {"unknown", "none", "public", "private", "sensitive"},
    "query_behavior": {"unknown", "unchanged", "error", "controlled_difference"},
}
_OPTIONAL_BOOLS = {"auth_required", "upload_public"}
_BOOLS = {"baseline_compared", "reproduced"}
_ROUTES = {"api", "search", "login", "upload", "admin", "users", "orders", "products",
           "download", "profile", "reset", "checkout"}
_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "TRACE"}
_AUTH = {"anonymous", "authenticated", "owner", "non_owner", "admin", "unknown"}
_HYPOTHESES = {"sqli", "idor", "xss", "upload", "ssrf", "path_traversal", "auth", "csrf"}
OBSERVATIONS_SCHEMA = {"type": "object", "additionalProperties": False, "properties": {
    **{key: {"enum": sorted(values)} for key, values in _ENUMS.items()},
    **{key: {"type": ["boolean", "null"]} for key in sorted(_OPTIONAL_BOOLS)},
    **{key: {"type": "boolean"} for key in sorted(_BOOLS)},
    "response_status": {"anyOf": [{"type": "integer", "minimum": 100, "maximum": 599}, {"type": "null"}]}}}


class ScoreContractError(ValueError):
    """Messages are fixed codes: upstream free text must never enter logs."""


def canonical_bytes(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def fingerprint(value) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def observations(value) -> dict:
    """Unknown fields are dropped; invalid values of known fields are rejected."""
    if not isinstance(value, dict):
        raise ScoreContractError("invalid_observations")
    result = {}
    for key, allowed in _ENUMS.items():
        field = value.get(key, "unknown")
        if not isinstance(field, str) or field not in allowed:
            raise ScoreContractError("invalid_observation_value")
        result[key] = field
    for key in _OPTIONAL_BOOLS:
        field = value.get(key)
        if field is not None and type(field) is not bool:
            raise ScoreContractError("invalid_observation_value")
        result[key] = field
    for key in _BOOLS:
        field = value.get(key, False)
        if type(field) is not bool:
            raise ScoreContractError("invalid_observation_value")
        result[key] = field
    status = value.get("response_status")
    if status is not None and (type(status) is not int or not 100 <= status <= 599):
        raise ScoreContractError("invalid_observation_value")
    result["response_status"] = status
    return result


def project_node(node: dict) -> dict:
    if (not isinstance(node, dict) or not isinstance(node.get("id"), str)
            or not re.fullmatch(r"N[0-9]{4,12}", node["id"])
            or node.get("kind") not in ("root", "surface", "candidate")):
        raise ScoreContractError("invalid_node")
    locator = node.get("locator", {})
    if not isinstance(locator, dict):
        raise ScoreContractError("invalid_locator")
    try:
        path = unquote(urlsplit(locator.get("url", "")).path).lower()
    except (TypeError, ValueError, AttributeError):
        raise ScoreContractError("invalid_locator") from None
    route_categories = sorted(set(re.split(r"[^a-z]+", path)) & _ROUTES)
    input_name = locator.get("input", "")
    input_category = ("search" if input_name in ("q", "search", "query") else
                      "object_id" if input_name in ("id", "uid", "user_id", "pid", "order_id") else
                      "file" if input_name in ("file", "upload", "attachment") else
                      "credential" if input_name in ("password", "username", "token") else
                      "none" if input_name == "" else "other")
    method = locator.get("method", "")
    auth = locator.get("auth_context", "unknown")
    hypothesis = node.get("hypothesis") or {}
    hypothesis_type = hypothesis.get("type") if isinstance(hypothesis, dict) else None
    return {"id": node["id"], "kind": node["kind"],
            "location": {"method": method if isinstance(method, str) and method in _METHODS else "OTHER",
                         "route_categories": route_categories, "input_category": input_category},
            "auth_context": auth if isinstance(auth, str) and auth in _AUTH else "unknown",
            "hypothesis_type": hypothesis_type if isinstance(hypothesis_type, str) and hypothesis_type in _HYPOTHESES else "other",
            "observations": observations(node.get("observations", {}))}


def build_score_request(model: str, parent: dict, siblings: list[dict], node_ids: list[str]) -> dict:
    if not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.:-]+", model):
        raise ScoreContractError("invalid_model_id")
    projected = [project_node(node) for node in siblings]
    nodes = {node["id"]: node for node in projected}
    if (len(nodes) != len(siblings) or not node_ids or len(node_ids) != len(set(node_ids))
            or not set(node_ids) <= nodes.keys()):
        raise ScoreContractError("invalid_question_mapping")
    return {"model": model, "provider": {"allow_fallbacks": False},
            "questions": {f"risk_{node_id}": {"type": "score", "criteria": list(CRITERIA),
                "instructions": f"Assess investigation priority for nodes.{node_id} in state, relative to its siblings. "
                "Use only these categorical observations; unknown is not evidence of safety or vulnerability. "
                "The ordered criteria jointly consider potential impact and observed feasibility. "
                "Do not treat an unverified observation or a risk score as a confirmed vulnerability."}
                for node_id in node_ids},
            "state": {"rubric_version": RUBRIC_VERSION, "projection_version": PROJECTION_VERSION,
                      "parent": project_node(parent), "nodes": nodes}}


def build_ordinal_request(model: str, parent: dict, siblings: list[dict], node_ids: list[str]) -> dict:
    if not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9_.:/-]{1,256}", model):
        raise ScoreContractError("invalid_model_id")
    projected = [project_node(node) for node in siblings]
    nodes = {node["id"]: node for node in projected}
    if (len(nodes) != len(siblings) or not node_ids or len(node_ids) != len(set(node_ids))
            or not set(node_ids) <= nodes.keys()):
        raise ScoreContractError("invalid_question_mapping")
    return {"model": model, "method": "ordinal_label", "criteria": list(CRITERIA),
            "node_ids": list(node_ids), "state": {"rubric_version": RUBRIC_VERSION,
            "projection_version": PROJECTION_VERSION, "parent": project_node(parent), "nodes": nodes}}


def is_finite_number(value) -> bool:
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _number(value, low=0.0, high=math.inf) -> float:
    if not is_finite_number(value) or not low <= value <= high:
        raise ScoreContractError("invalid_number")
    return float(value)


def validate_answer(answer: dict) -> dict:
    if not isinstance(answer, dict) or answer.get("type") != "score":
        raise ScoreContractError("invalid_answer")
    count = len(CRITERIA)
    raw = _number(answer.get("score"), 0, count - 1)
    probabilities = answer.get("probabilities")
    keys = {str(i) for i in range(count)}
    if not isinstance(probabilities, dict) or set(probabilities) != keys:
        raise ScoreContractError("invalid_probabilities")
    probabilities = {str(i): _number(probabilities[str(i)], 0, 1) for i in range(count)}
    if abs(sum(probabilities.values()) - 1) > PROBABILITY_SUM_TOLERANCE:
        raise ScoreContractError("invalid_probability_sum")
    weighted = sum(i * probabilities[str(i)] for i in range(count))
    if abs(raw - weighted) > WEIGHTED_SCORE_TOLERANCE:
        raise ScoreContractError("inconsistent_weighted_score")
    # There must exist ONE probability distribution, summing to 1, inside
    # all rounding intervals whose weighted sum can round to the given score.
    lower = [max(0, probabilities[str(i)] - ROUNDING_HALF_UNIT) for i in range(count)]
    upper = [min(1, probabilities[str(i)] + ROUNDING_HALF_UNIT) for i in range(count)]
    if sum(lower) > 1 + 1e-6 or sum(upper) < 1 - 1e-6:
        raise ScoreContractError("invalid_probability_sum")
    def extreme(indices):
        values = list(lower)
        remaining = max(0, 1 - sum(values))
        for i in indices:
            added = min(remaining, upper[i] - values[i])
            values[i] += added
            remaining -= added
        return sum(i * value for i, value in enumerate(values))
    minimum, maximum = extreme(range(count)), extreme(reversed(range(count)))
    if raw + ROUNDING_HALF_UNIT < minimum - 1e-6 or raw - ROUNDING_HALF_UNIT > maximum + 1e-6:
        raise ScoreContractError("inconsistent_weighted_score")
    if answer.get("legend") != {str(i): value for i, value in enumerate(CRITERIA)}:
        raise ScoreContractError("invalid_legend")
    return {"score": 100 * raw / (count - 1), "raw_score": raw,
            "score_method": "probability_weighted", "probabilities": probabilities,
            "confidence": _number(answer.get("confidence"), 0, 1), "rounding_profile": "decimal-2"}


def validate_ordinal_answer(label: int) -> dict:
    if type(label) is not int or not 0 <= label < len(CRITERIA):
        raise ScoreContractError("invalid_ordinal_label")
    return {"score": 100.0 * label / (len(CRITERIA) - 1), "raw_score": label,
            "score_method": "ordinal_label", "label": label,
            "probabilities": None, "confidence": None, "rounding_profile": None}


def validate_usage(value) -> dict:
    if not isinstance(value, dict):
        raise ScoreContractError("invalid_usage")
    for name in ("input_tokens", "output_tokens"):
        if type(value.get(name)) is not int or value[name] < 0:
            raise ScoreContractError("invalid_usage")
    return {"input_tokens": value["input_tokens"], "output_tokens": value["output_tokens"],
            "cost": _number(value.get("cost"))}


def validate_response(response: dict, allowed_model_ids: set[str], node_ids: list[str]) -> dict:
    if not isinstance(response, dict) or not isinstance(response.get("model"), str) or response["model"] not in allowed_model_ids:
        raise ScoreContractError("model_mismatch")
    usage = validate_usage(response.get("usage"))
    answers = response.get("answers")
    if not isinstance(answers, dict) or set(answers) - {f"risk_{key}" for key in node_ids}:
        raise ScoreContractError("invalid_answer_mapping")
    scores, errors, diagnostics = {}, {}, {}
    for node_id in node_ids:
        try:
            scores[node_id] = validate_answer(answers.get(f"risk_{node_id}"))
        except ScoreContractError as exc:
            errors[node_id] = "invalid_answer"
            raw = answers.get(f"risk_{node_id}")
            raw = raw if isinstance(raw, dict) else {}
            probabilities = raw.get("probabilities")
            probabilities = probabilities if isinstance(probabilities, dict) else {}
            def safe_number(value):
                return value if is_finite_number(value) else None
            diagnostics[node_id] = {"reason": str(exc), "score": safe_number(raw.get("score")),
                "confidence": safe_number(raw.get("confidence")),
                "probabilities": {str(i): safe_number(probabilities.get(str(i))) for i in range(len(CRITERIA))},
                "legend_matches": raw.get("legend") == {str(i): value for i, value in enumerate(CRITERIA)}}
    # Only validated fields survive. No provider-authored prose is persisted.
    return {"model": response["model"], "usage": usage, "scores": scores, "node_errors": errors,
            "diagnostics": diagnostics}

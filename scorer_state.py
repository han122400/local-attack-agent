"""Pure scoring state/replay: no model calls, no automatic resume."""

from __future__ import annotations

import copy
import math
import re

from decision_provider import ProviderError, estimated_cost, validate_pin
from experiment_config import validate_experiment_config
from risk_rubric import (CONTRACT_VERSION, PROJECTION_VERSION, RUBRIC_VERSION, CRITERIA,
                         ScoreContractError, build_score_request, canonical_bytes, fingerprint,
                         project_node, validate_answer, validate_usage)
from v4_contracts import ContractError


SCORING_EVENTS = {"scorer_configured", "scorer_request_started", "scorer_request_finished",
                  "scorer_batch_applied", "scorer_halted"}
SETTINGS_VERSION = "scorer-settings-v1"
DEFAULT_SETTINGS = {"batch_size": 8, "max_input_bytes": 16384, "max_calls": 20,
                    "max_calls_per_parent": 4, "max_retries": 1, "timeout_seconds": 20,
                    "time_budget_seconds": 120, "max_cost_usd": .01,
                    "rubric_version": RUBRIC_VERSION, "reevaluation_policy": PROJECTION_VERSION}
_UNSCORED = {"invalid_input", "invalid_answer", "parent_limit", "input_limit"}
_STOPS = {"call_budget", "cost_budget", "time_budget", "scorer_unavailable"}


def empty_scorer() -> dict:
    return {"configuration": None, "requests": {}, "batches": {}, "halt": None}


def effective_settings(metadata: dict) -> dict:
    config = validate_experiment_config(metadata)
    if config["experiment_mode"] != "v4" or config["scorer_provider"] != "openrouter":
        raise ContractError("A3 supports only the OpenRouter Decisions scorer; local support belongs to B")
    settings = {**DEFAULT_SETTINGS, **config["scorer_settings"]}
    if "max_calls_per_parent" not in config["scorer_settings"]:
        settings["max_calls_per_parent"] = min(settings["max_calls_per_parent"], settings["max_calls"])
    validate_experiment_config({**config, "scorer_settings": settings})
    if settings["rubric_version"] != RUBRIC_VERSION or settings["reevaluation_policy"] != PROJECTION_VERSION:
        raise ContractError("Unsupported scoring contract version")
    return settings


def context(tree: dict, parent_id: str, configuration: dict) -> dict:
    parent = tree["nodes"][parent_id]
    siblings = [tree["nodes"][key] for key in parent["child_ids"]]
    projected, valid, invalid = [], [], {}
    for node in siblings:
        try:
            item = project_node(node)
            valid.append(node)
        except ScoreContractError:
            item = {"id": node["id"], "invalid_input": True}
            invalid[node["id"]] = "invalid_input"
        projected.append({"projection": item, "locator_hash": fingerprint(node["locator"])})
    # Completed siblings stay in the shared context. Visits, attempts, free-text
    # claims, and evidence attachments alone do not invalidate a score.
    value = {"configuration": configuration, "parent": project_node(parent),
             "parent_locator_hash": fingerprint(parent["locator"]), "siblings": projected}
    eligible = [node["id"] for node in siblings if node["exploration_state"] not in ("done", "blocked")]
    return {"key": fingerprint(value), "parent": parent, "siblings": valid, "eligible": eligible, "invalid": invalid}


def usage(state: dict) -> dict:
    requests = [item for item in state["requests"].values() if item.get("finish", {}).get("attempted", True)]
    known = [item["finish"]["usage"] for item in requests if item.get("finish", {}).get("usage") is not None]
    unknown = [item for item in requests if item.get("finish", {}).get("usage") is None]
    known_cost = sum(item["cost"] for item in known)
    reserved = sum(item["estimated_cost_usd"] for item in unknown)
    seconds = sum(item["finish"]["elapsed_seconds"] if "finish" in item else item["timeout_seconds"] for item in requests)
    seconds += (state["configuration"] or {}).get("catalog_elapsed_seconds", 0)
    return {"requests": len(requests), "questions": sum(len(item["node_ids"]) for item in requests),
            "completed_requests": sum("finish" in item for item in requests),
            "pending_requests": sum("finish" not in item for item in requests),
            "input_tokens": None if unknown else sum(item["input_tokens"] for item in known),
            "output_tokens": None if unknown else sum(item["output_tokens"] for item in known),
            "known_input_tokens": sum(item["input_tokens"] for item in known),
            "known_output_tokens": sum(item["output_tokens"] for item in known),
            "cost_usd": None if unknown else known_cost, "known_cost_usd": known_cost,
            "unknown_cost_requests": len(unknown), "reserved_cost_usd": reserved,
            "accounted_cost_usd": known_cost + reserved, "elapsed_seconds": seconds}


def numeric(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ContractError("Invalid scoring accounting value")
    return value


def _score(value: dict) -> dict:
    parsed = validate_answer({"type": "score", "score": value["raw_score"], "probabilities": value["probabilities"],
                              "confidence": value["confidence"], "legend": {str(i): text for i, text in enumerate(CRITERIA)}})
    if value != parsed:
        raise ContractError("Invalid normalized score")
    return parsed


def outcomes(state: dict, key: str) -> tuple[dict, dict]:
    scores, failures = {}, {}
    for item in state["requests"].values():
        if item["context_key"] != key:
            continue
        result = item.get("finish", {}).get("result")
        if result:
            scores.update(result["scores"])
            for node_id in result["node_errors"]:
                failures[node_id] = failures.get(node_id, 0) + 1
    return scores, failures


def apply_scoring_event(tree: dict, kind: str, payload: dict) -> None:
    state = tree["scorer"]
    if kind == "scorer_configured":
        if state["configuration"] is not None:
            raise ContractError("Scorer configuration is immutable")
        try:
            pin = validate_pin(payload["model_pin"])
        except ProviderError:
            raise ContractError("Invalid saved scorer model pin") from None
        settings = effective_settings({"experiment_mode": "v4", "scorer_provider": payload["provider"],
                                       "scorer_model": pin["id"], "scorer_settings": payload["settings"]})
        if (settings != payload["settings"] or payload["settings_version"] != SETTINGS_VERSION or payload["contract_version"] != CONTRACT_VERSION
                or payload["projection_version"] != PROJECTION_VERSION or payload["rubric_version"] != RUBRIC_VERSION):
            raise ContractError("Unsupported scorer configuration")
        numeric(payload["catalog_elapsed_seconds"])
        state["configuration"] = copy.deepcopy(payload)
        return
    if kind == "scorer_halted":
        if payload["reason"] not in _STOPS:
            raise ContractError("Invalid scorer stop reason")
        if state["halt"] is not None:
            raise ContractError("Scorer already halted")
        state["halt"] = copy.deepcopy(payload)
        return
    configuration = state["configuration"]
    if configuration is None:
        raise ContractError("Scorer has not been configured")
    if kind == "scorer_request_started":
        if state["halt"] or any("finish" not in item for item in state["requests"].values()):
            raise ContractError("Scorer is halted or already has an in-flight request")
        request_id = payload["request_id"]
        if request_id != f"S{len(state['requests']) + 1:06d}":
            raise ContractError("Invalid scoring request identity")
        current = context(tree, payload["parent_id"], configuration)
        ids = payload["node_ids"]
        if (payload["context_key"] != current["key"] or not isinstance(ids, list) or not ids
                or len(set(ids)) != len(ids) or not set(ids) <= set(current["eligible"])):
            raise ContractError("Stale or invalid scoring candidates")
        expected = build_score_request(configuration["model_pin"]["id"], current["parent"], current["siblings"], ids)
        if payload["request"] != expected or payload["input_hash"] != fingerprint(expected):
            raise ContractError("Scoring input/provenance mismatch")
        settings = configuration["settings"]
        if len(ids) > settings["batch_size"] or len(canonical_bytes(expected)) > settings["max_input_bytes"]:
            raise ContractError("Scoring input exceeds fixed limits")
        numeric(payload["estimated_cost_usd"])
        numeric(payload["timeout_seconds"])
        totals = usage(state)
        if (not math.isclose(payload["estimated_cost_usd"], estimated_cost(configuration["model_pin"], expected), rel_tol=1e-12, abs_tol=1e-12)
                or totals["requests"] >= settings["max_calls"]
                or sum(item["parent_id"] == payload["parent_id"] for item in state["requests"].values()) >= settings["max_calls_per_parent"]
                or totals["accounted_cost_usd"] + payload["estimated_cost_usd"] > settings["max_cost_usd"]):
            raise ContractError("Invalid or exhausted scoring budget reservation")
        if not 0 < payload["timeout_seconds"] <= min(settings["timeout_seconds"], settings["time_budget_seconds"] - totals["elapsed_seconds"]):
            raise ContractError("Invalid scorer timeout")
        state["requests"][request_id] = copy.deepcopy(payload)
    elif kind == "scorer_request_finished":
        request = state["requests"][payload["request_id"]]
        if "finish" in request or type(payload["attempted"]) is not bool:
            raise ContractError("Duplicate or invalid scorer request result")
        numeric(payload["elapsed_seconds"])
        if payload["usage"] is not None:
            validate_usage(payload["usage"])
        result = payload["result"]
        if result is not None:
            if not payload["attempted"] or payload["error"] is not None or result["model"] not in (
                    configuration["model_pin"]["id"], configuration["model_pin"]["canonical_slug"]):
                raise ContractError("Invalid scorer response provenance")
            ids = set(request["node_ids"])
            scores, errors = result["scores"], result["node_errors"]
            if (set(scores) & set(errors) or set(scores) | set(errors) != ids
                    or any(value != "invalid_answer" for value in errors.values()) or payload["usage"] is None
                    or payload["usage"] != result["usage"]):
                raise ContractError("Invalid scorer answer mapping/usage")
            for score in scores.values():
                _score(score)
        else:
            error = payload["error"]
            if (not isinstance(error, dict) or not isinstance(error.get("code"), str)
                    or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", error["code"]) or type(error.get("retryable")) is not bool):
                raise ContractError("Invalid provider failure")
        request["finish"] = copy.deepcopy(payload)
    elif kind == "scorer_batch_applied":
        current = context(tree, payload["parent_id"], configuration)
        if current["key"] != payload["context_key"] or set(payload["outcomes"]) != set(current["eligible"]) or state["halt"]:
            raise ContractError("Stale or incomplete scoring batch")
        completed, _failures = outcomes(state, current["key"])
        for node_id, risk in payload["outcomes"].items():
            if risk["status"] == "scored":
                if risk["value"] != completed.get(node_id):
                    raise ContractError("Score has no matching completed request")
                value = _score(risk["value"])
                source = next(item for item in reversed(list(state["requests"].values())) if item["context_key"] == current["key"]
                              and node_id in (item.get("finish", {}).get("result") or {}).get("scores", {}))
                tree["nodes"][node_id]["risk"] = {"status": "scored", **value, "context_key": current["key"],
                    "model": source["finish"]["result"]["model"], "requested_model": configuration["model_pin"]["id"],
                    "source_request_id": source["request_id"], "rubric_version": RUBRIC_VERSION,
                    "contract_version": CONTRACT_VERSION}
            elif risk["status"] == "unscored" and risk["reason"] in _UNSCORED:
                tree["nodes"][node_id]["risk"] = {"status": "unscored", "score": None,
                    "reason": risk["reason"], "context_key": current["key"]}
            else:
                raise ContractError("Invalid scoring outcome")
        state["batches"][current["key"]] = copy.deepcopy(payload["outcomes"])
    else:
        raise ContractError("Unsupported scorer event")

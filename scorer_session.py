"""Lazy sibling scoring. Does not select, execute, or confirm a target node."""

from __future__ import annotations

import copy
import time

from decision_provider import OpenRouterDecisions, ProviderError, estimated_cost, utc_now
from risk_rubric import (CONTRACT_VERSION, PROJECTION_VERSION, RUBRIC_VERSION, build_score_request,
                         canonical_bytes, fingerprint)
from scorer_state import SETTINGS_VERSION, context, effective_settings, outcomes, usage
from v4_contracts import ContractError


class RiskScorer:
    def __init__(self, tree, *, adapter=None, clock=time.monotonic):
        self.tree, self.log = tree, tree.log
        self.adapter = adapter or OpenRouterDecisions()
        self._owns_adapter = adapter is None
        self.clock = clock

    def close(self):
        if self._owns_adapter:
            self.adapter.close()

    def usage(self):
        return usage(self.tree.refresh()["scorer"])

    def configure(self) -> dict:
        state = self.tree.refresh()["scorer"]
        settings = effective_settings(self.log.metadata)
        existing = state["configuration"]
        if existing:
            if (existing["provider"] != self.log.metadata["scorer_provider"]
                    or existing["model_pin"]["id"] != self.log.metadata["scorer_model"]
                    or existing["settings"] != settings):
                raise ContractError("A run cannot change its scorer model/provider/settings")
            return existing
        started = self.clock()
        pin = self.adapter.resolve(self.log.metadata["scorer_model"], timeout_seconds=settings["timeout_seconds"])
        if pin["id"] != self.log.metadata["scorer_model"]:
            raise ContractError("Catalog resolved a different scorer model")
        configuration = {"provider": "openrouter", "model_pin": pin, "settings": settings,
                         "settings_version": SETTINGS_VERSION,
                         "rubric_version": RUBRIC_VERSION, "projection_version": PROJECTION_VERSION,
                         "contract_version": CONTRACT_VERSION, "catalog_elapsed_seconds": self.clock() - started}
        self.tree.record_scoring("scorer_configured", configuration)
        return configuration

    def _stop(self, reason: str, *, persist=True) -> dict:
        if persist and not self.tree.refresh()["scorer"]["halt"]:
            self.tree.record_scoring("scorer_halted", {"reason": reason})
        return {"ready": False, "stop_reason": reason, "ordered_node_ids": [], "scores": {}, "unscored": {}}

    @staticmethod
    def _ready(current: dict, values: dict) -> dict:
        scores = {key: values[key]["value"]["score"] for key in current["eligible"] if values[key]["status"] == "scored"}
        unscored = {key: values[key]["reason"] for key in current["eligible"] if values[key]["status"] == "unscored"}
        order = {key: i for i, key in enumerate(current["eligible"])}
        ids = sorted(current["eligible"], key=lambda key: (key not in scores, -scores.get(key, 0), order[key], key))
        return {"ready": True, "stop_reason": None, "context_key": current["key"],
                "ordered_node_ids": ids, "scores": scores, "unscored": unscored,
                "fallback_node_ids": [key for key in ids if key in unscored]}

    @staticmethod
    def _provider_failure(state):
        count, last = 0, None
        for request in reversed(list(state["requests"].values())):
            finish = request.get("finish")
            if finish is None or finish["error"] is None:
                break
            last = last or finish["error"]
            count += 1
        return last, count

    def score_children(self, parent_id: str) -> dict:
        tree = self.tree.refresh()
        if tree["scorer"]["halt"]:
            return self._stop(tree["scorer"]["halt"]["reason"], persist=False)
        if parent_id not in tree["nodes"]:
            raise ContractError("Unknown scoring parent")
        if not tree["nodes"][parent_id]["child_ids"]:
            return {"ready": True, "stop_reason": None, "ordered_node_ids": [], "scores": {}, "unscored": {}, "fallback_node_ids": []}
        try:
            config = self.configure()
        except ProviderError:
            return self._stop("scorer_unavailable")
        settings, pin = config["settings"], config["model_pin"]
        initial = context(self.tree.refresh(), parent_id, config)
        local_errors = {key: value for key, value in initial["invalid"].items() if key in initial["eligible"]}
        while True:
            tree = self.tree.refresh()
            state = tree["scorer"]
            if state["halt"]:
                return self._stop(state["halt"]["reason"], persist=False)
            # A pending reservation may be an interrupted/billed call. Replays
            # and new scorer objects must never reissue it automatically.
            if any("finish" not in item for item in state["requests"].values()):
                return self._stop("scorer_unavailable", persist=False)
            current = context(tree, parent_id, config)
            if current["key"] != initial["key"]:
                return self._stop("context_changed", persist=False)
            accounting = usage(state)
            if accounting["accounted_cost_usd"] > settings["max_cost_usd"]:
                return self._stop("cost_budget")
            if accounting["elapsed_seconds"] > settings["time_budget_seconds"]:
                return self._stop("time_budget")
            error, failures = self._provider_failure(state)
            if error and (not error["retryable"] or failures > settings["max_retries"]):
                return self._stop("scorer_unavailable")
            cached = state["batches"].get(current["key"])
            if cached is not None and set(current["eligible"]) <= cached.keys():
                if any(tree["nodes"][key]["risk"].get("context_key") != current["key"] for key in current["eligible"]):
                    self.tree.record_scoring("scorer_batch_applied", {"parent_id": parent_id,
                        "context_key": current["key"], "outcomes": {key: cached[key] for key in current["eligible"]}})
                return self._ready(current, cached)
            scores, failed_nodes = outcomes(state, current["key"])
            for key, count in failed_nodes.items():
                if count > settings["max_retries"] and key not in scores:
                    local_errors[key] = "invalid_answer"
            missing = [key for key in current["eligible"] if key not in scores and key not in local_errors]
            if not missing:
                values = {key: {"status": "scored", "value": scores[key]} if key in scores else
                           {"status": "unscored", "reason": local_errors[key]} for key in current["eligible"]}
                self.tree.record_scoring("scorer_batch_applied", {"parent_id": parent_id,
                    "context_key": current["key"], "outcomes": values})
                return self._ready(current, values)
            # Global limits win over the parent cap. Never offer a partially
            # evaluated sibling batch as selectable on a global budget stop.
            if accounting["requests"] >= settings["max_calls"]:
                return self._stop("call_budget")
            if accounting["elapsed_seconds"] >= settings["time_budget_seconds"]:
                return self._stop("time_budget")
            parent_calls = sum(item["parent_id"] == parent_id for item in state["requests"].values())
            if parent_calls >= settings["max_calls_per_parent"]:
                if error:
                    return self._stop("scorer_unavailable")
                local_errors.update({key: "parent_limit" for key in missing})
                continue
            ids = missing[:settings["batch_size"]]
            while ids:
                payload = build_score_request(pin["id"], current["parent"], current["siblings"], ids)
                if len(canonical_bytes(payload)) <= settings["max_input_bytes"]:
                    break
                ids.pop()
            if not ids:
                # Keep the shared context intact. Dropping arbitrary siblings
                # would change the comparison; an oversized node stays unknown.
                local_errors[missing[0]] = "input_limit"
                continue
            estimate = estimated_cost(pin, payload)
            if accounting["accounted_cost_usd"] + estimate > settings["max_cost_usd"]:
                return self._stop("cost_budget")
            timeout = min(settings["timeout_seconds"], settings["time_budget_seconds"] - accounting["elapsed_seconds"])
            request_id = f"S{len(state['requests']) + 1:06d}"
            self.tree.record_scoring("scorer_request_started", {"request_id": request_id, "parent_id": parent_id,
                "context_key": current["key"], "node_ids": ids, "request": payload, "input_hash": fingerprint(payload),
                "estimated_cost_usd": estimate, "timeout_seconds": timeout, "started_at": utc_now()})
            started = self.clock()
            try:
                result = self.adapter.evaluate(pin, copy.deepcopy(current["parent"]), copy.deepcopy(current["siblings"]), list(ids),
                                               max_input_bytes=settings["max_input_bytes"], timeout_seconds=timeout)
                result = {key: result[key] for key in ("model", "usage", "scores", "node_errors")}
                finished = {"request_id": request_id, "attempted": True, "result": result,
                            "usage": result["usage"], "error": None}
            except ProviderError as exc:
                finished = {"request_id": request_id, "attempted": exc.attempted, "result": None,
                            "usage": exc.usage, "error": {"code": exc.code, "retryable": exc.retryable}}
            finished["elapsed_seconds"] = self.clock() - started
            self.tree.record_scoring("scorer_request_finished", finished)

"""Greedy depth-first orchestration, separate from v3 and deterministic replay."""

import hashlib
import json
import re
import threading
import time
from dataclasses import replace
from urllib.parse import urlsplit

from engine.code_checks import check_generated_code
from engine.harness import Harness, ModelFailure
from engine.model_providers import is_openrouter
from engine.planning import load_prompt
from engine.session_log import atomic_write_json
from engine.scorer_session import RiskScorer
from engine.usage import summarize_usage
from engine.contracts import (ContractError, MAX_RESULT_BYTES, RESULT_SCHEMA, dedup_key, evidence_path,
                          normalize_url, validate_result)
from engine.policy import V4BudgetStop, execution_settings
from engine.tree import (TreeStore, MAX_EVIDENCE_BYTES, _json_result, _read_bounded, _safe_path,
                     latest_observation, investigation_gaps, seed_dependency_invalidation_seq)
from engine.risk_rubric import observations, OBSERVATIONS_SCHEMA


def seed_dispatchable(state, node_id, target):
    """Whether a same-origin path has its seeded prerequisites verified."""
    return seed_execution_path(state, node_id, target) is not None


def seed_execution_path(state, node_id, target):
    """Find a verified path; only explicitly verified alternate edges may bypass a primary dependency."""
    def origin(url):
        parts = urlsplit(normalize_url(url))
        return parts.scheme, parts.hostname, parts.port or (443 if parts.scheme == "https" else 80)

    allowed = origin(target)
    def find(current_id, visited):
        if current_id in visited or current_id not in state["nodes"]:
            return None
        node = state["nodes"][current_id]
        if node.get("exploration_state") == "blocked" or origin(node["locator"]["url"]) != allowed:
            return None
        parent_id = node.get("parent_id")
        if parent_id is None:
            return [current_id] if current_id == state.get("root_id", current_id) else None
        paths = node.get("discovery_paths", [])
        primary = next((path for path in paths if path["parent_id"] == parent_id), None)
        if primary is None:
            return None
        verified = node.get("seed_enabled_by_verified", {})
        alternatives = [path for path in paths if path != primary and path["relation"] == "enabled_by"
                        and path["parent_id"] in verified]
        for path in [primary, *alternatives]:
            if path["relation"] == "enabled_by" and node.get("seeded") and path["parent_id"] not in verified:
                continue
            prefix = find(path["parent_id"], visited | {current_id})
            if prefix is not None:
                return [*prefix, current_id]
        return None
    return find(node_id, set())


def needs_seed_prerequisite_observation(state, node_id):
    """A reused node must be investigated if a live child relies on it."""
    node = state["nodes"][node_id]
    if node.get("investigation_state") != "reused":
        return False
    # Alternate discovery paths do not appear in the primary child_ids list.
    # They still need a current-run parent observation before operator review.
    for child in state["nodes"].values():
        if (child.get("seeded") and child.get("exploration_state") not in ("done", "blocked")
                and any(path["parent_id"] == node_id and path["relation"] == "enabled_by"
                        for path in child.get("discovery_paths", []))
                and node_id not in child.get("seed_enabled_by_verified", [])):
            return True
    return False


class BudgetGate:
    def __init__(self, log, *, stop_event, clock=time.monotonic):
        self.log, self.stop_event, self.clock = log, stop_event, clock
        self.settings = execution_settings(log.metadata.get("v4_settings"))
        self.budgets = log.metadata["budgets"]
        self.eager_call_limit = None
        self.deadline = clock() + self.budgets["seconds"]
        self.deep_reserve = min(self.settings["deep_reserve_calls"], self.budgets["model_calls"] // 4)
        log.update(v4_budget_policy={"version": "v4-budget-v3", "deep_reserve_calls": self.deep_reserve,
                                    "node_limit_policy": "consecutive_unproductive_results"})

    def usage(self):
        return summarize_usage(self.log.snapshot_events(), TreeStore(self.log).refresh(),
            elapsed_seconds=max(0, self.budgets["seconds"] - (self.deadline - self.clock())))

    def check(self):
        if self.stop_event.is_set():
            raise V4BudgetStop("user_stopped")
        if self.clock() >= self.deadline:
            raise V4BudgetStop("time_budget")
        value = self.usage()
        self.log.update(v4_usage=value)
        if value["accounted_cost_usd"] > self.settings["max_cost_usd"]:
            raise V4BudgetStop("cost_budget")
        return value

    def admit(self, phase, estimate, timeout):
        value = self.check()
        if self.eager_call_limit is not None and phase in ("execution", "scorer") and value["model_calls"] >= self.eager_call_limit:
            raise V4BudgetStop("eager_limit")
        if phase in ("execution", "scorer") and value["model_calls"] >= self.budgets["model_calls"]:
            raise V4BudgetStop("call_budget")
        if value["accounted_cost_usd"] + estimate > self.settings["max_cost_usd"]:
            raise V4BudgetStop("cost_budget")
        return min(timeout, self.deadline - self.clock())

    def before_model(self, call_id, payload):
        remote = is_openrouter(payload["model"])
        estimate = self.settings["remote_call_reserve_usd"] if remote else 0.0
        self.admit("execution", estimate, self.budgets["seconds"])
        context = json.loads(payload["messages"][1]["content"])
        state = TreeStore(self.log).refresh()
        deeper_work = any(node["parent_id"] is not None and node["exploration_state"] not in ("done", "blocked")
                          for node in state["nodes"].values())
        if (context.get("node", {}).get("id") == "N0001" and deeper_work
                and self.usage()["model_calls"] >= self.budgets["model_calls"] - self.deep_reserve):
            raise V4BudgetStop("node_limit")
        self.log.append("v4_execution_reserved", {"call_id": call_id, "remote": remote, "estimate_usd": estimate})


class V4Runner:
    # Give an operator a bounded opportunity to review evidence without
    # exceeding the run's ordinary time budget.
    seed_review_wait_seconds = 30.0

    def __init__(self, log, runtime, *, transport=None, scorer_adapter=None, stop_event=None, clock=time.monotonic):
        self.log, self.runtime = log, runtime
        self.stop_event = stop_event or threading.Event()
        self.gate = BudgetGate(log, stop_event=self.stop_event, clock=clock)
        self.settings = self.gate.settings
        self.tree = TreeStore(log)
        if not self.tree.refresh()["root_id"]:
            self.tree.initialize(log.metadata["target"])
        prompt, digest = load_prompt("v4")
        self.harness = Harness(log, transport=transport, max_calls=log.metadata["budgets"]["model_calls"],
            max_decode_attempts=2, max_transport_retries=1, stop_requested=self.stop_event.is_set,
            system_prompt=prompt, system_prompt_version="v4", system_prompt_sha256=digest, before_request=self._before_model)
        self.scorer = RiskScorer(self.tree, adapter=scorer_adapter, admit_request=self.gate.admit)
        self.history = []
        self._discovery_cache = {}
        from engine.planning import context_char_budget
        self.context_budget = context_char_budget(log.metadata["model_settings"]["options"].get("num_ctx", 8192), prompt)

    def _before_model(self, call_id, payload):
        self.gate.before_model(call_id, payload)
        # This budget is the application's character-based approximation for
        # non-system messages, not the model's actual token or RAM usage.
        chars = sum(len(message.get("content", "")) for message in payload["messages"][1:])
        self.log.update(context_chars=chars, context_budget=self.context_budget)

    def _context(self, node_id, *, unproductive=0, node_tools=0):
        state = self.tree.refresh()
        node = state["nodes"][node_id]
        def public(item):
            return {key: item[key] for key in ("id", "kind", "locator", "hypothesis", "observations")}
        route = seed_execution_path(state, node_id, self.log.metadata["target"])
        if route is None:
            raise ContractError("Node has no eligible scope and dependency path")
        path = [public(state["nodes"][key]) for key in route[:-1]]
        attempt_id = f"A{len(state['attempts']) + 1:04d}"
        siblings = state["nodes"][node["parent_id"]]["child_ids"] if node["parent_id"] else []
        ranks = []
        for key in siblings:
            risk = state["nodes"][key]["risk"]
            rank = {"node_id": key, "score": risk.get("score") if risk["status"] == "scored" else None,
                    "status": risk["status"], "reason": risk.get("reason")}
            if risk["status"] != "scored" and risk.get("score") is not None:
                rank["previous_score"] = risk["score"]
            ranks.append(rank)
        ranks.sort(key=lambda item: (item["score"] is None, -(item["score"] or 0), siblings.index(item["node_id"])))
        previous = latest_observation(state, node_id)
        if previous:
            previous = {"attempt_id": previous["attempt_id"],
                "result_path": f"work/node-results/{previous['attempt_id']}.json",
                "claim": previous["claim"][:1000], "proposed_verdict": previous["proposed_verdict"],
                "next_observation": previous["next_observation"],
                "evidence_refs": [{"path": ref["path"], "description": ref["description"][:200]}
                                  for ref in previous["evidence_refs"][:8]],
                "details_truncated": len(previous["claim"]) > 1000 or len(previous["evidence_refs"]) > 8
                                     or any(len(ref["description"]) > 200 for ref in previous["evidence_refs"])}
        value = {"goal": self.log.metadata["goal"], "target": self.log.metadata["target"], "node": public(node),
                "parent_path": path[-8:], "runtime_path": str(self.runtime.runtime_dir), "python": self.runtime.python,
                "attempt": {"attempt_id": attempt_id, "node_id": node_id, "result_path": f"work/node-results/{attempt_id}.json"},
                "result_template": {"schema_version": 1, "attempt_id": attempt_id, "node_id": node_id,
                                    "discovered": [], "proposed_verdict": "inconclusive", "claim": "",
                                    "evidence_refs": [], "next_observation": "", "observations": observations({})},
                "result_schema": RESULT_SCHEMA, "previous_result": previous,
                "discovery_evidence": self._discovery_evidence(node_id, state),
                "discovery_context": self._discovery_context(node_id, state),
                "recent": self.history[-2:], "sibling_ranks": ranks[:20], "total_siblings": len(siblings),
                "remaining": {"seconds": max(0, int(self.gate.deadline - self.gate.clock())),
                              "model_calls": self.gate.budgets["model_calls"] - self.gate.usage()["model_calls"],
                              "node_attempts": max(0, self.settings["node_attempts"] - unproductive),
                              "node_tool_calls": max(0, self.settings["node_tool_calls"] - node_tools)}}
        from engine.observation_verification import POLICIES, SHAPE_POLICY
        policy = (state["scorer"].get("configuration") or {}).get("projection_version")
        policy = policy or (self.log.metadata.get("scorer_settings") or {}).get("reevaluation_policy")
        if policy in POLICIES:
            value["http_capture"] = {
                "passive_http": "For HTTP targets, proxy-aware clients are recorded without a request helper. "
                "HARNESS_HTTP_CAPTURE_INDEX names an asynchronous JSON view of this exec's requests "
                "(request_id and facts). It is a receipt lookup, not authoritative evidence. "
                "Direct HTTPS outside audit_http/request remains unverified. Tools may bypass the proxy.",
                "https_browser_audit": "Actual HTTPS browser response events produce visibility-only parsed "
                "audit files, not literal evidence. They cannot support observations, scores, or verified reports. "
                "For data-bearing HTTPS measurements that need verification, plan the request through audit_http "
                "(run_python) or request (run_command) and cite its request_id. Do not use route.fetch or replay "
                "state-changing requests merely to obtain capture proof.",
                "python": "await audit_http(url, method='GET', headers={}, body=b'') returns request_id, status, body, json",
                "command": "from http_capture_client import request; response = request(url, method='GET', headers={}, body=b'')",
                "evidence": "Use observation_evidence[field] = {'request_ids': [response['request_id']]} for status. "
                "For query_behavior/reproduced/baseline_compared use {'baseline': [id1,id2], 'test': [id3,id4], "
                "'json_pointer': '/data'} for repeated GET JSON array counts with only the node input changed. "
                "Unknown or uncaptured observations remain unverified. Supply evidence per discovery when applicable."}
            if policy == SHAPE_POLICY:
                value["http_capture"]["evidence"] += (
                    " This policy also compares bounded JSON object keys, emptiness, and type at json_pointer; "
                    "use an empty pointer for the root object. Reflected input keys and scalar values do not count.")
            if node.get("origin") == "unverified_lead":
                source = state["attempts"][node["source_attempt_id"]]
                source_fields = source.get("observation_verification", {}).get("fields", {})
                value["verification_task"] = {
                    "source_attempt_id": node["source_attempt_id"],
                    "capture_request_ids": node["capture_request_ids"],
                    "claimed_signal": node["unverified_signal"],
                    "source_check": source_fields.get(node["unverified_field"], {}),
                    "instruction": "Investigate this unverified lead once. Use injected audit_http(url, params={...}) "
                    "in run_python or http_capture_client.request(url, params={...}) in run_command. "
                    "Repeat matched baseline and test requests, cite request_id values, and state what remains "
                    "unverified. Do not treat the source claim as supported evidence."}
        while len(json.dumps(value, ensure_ascii=False)) > self.context_budget and (value["recent"] or value["parent_path"]):
            (value["recent"] if value["recent"] else value["parent_path"]).pop(0)
        while len(json.dumps(value, ensure_ascii=False)) > self.context_budget and previous and previous["evidence_refs"]:
            previous["evidence_refs"].pop()
            previous["details_truncated"] = True
        if len(json.dumps(value, ensure_ascii=False)) > self.context_budget and previous:
            previous["claim"], previous["details_truncated"] = "", True
        provenance = value["discovery_evidence"]
        while len(json.dumps(value, ensure_ascii=False)) > self.context_budget and provenance and provenance["evidence_refs"]:
            provenance["evidence_refs"].pop()
            provenance["truncated"] = True
        if len(json.dumps(value, ensure_ascii=False)) > self.context_budget and provenance:
            provenance["claim"], provenance["truncated"] = "", True
        discovery = value["discovery_context"]
        # Items are newest-first: drop the oldest, keeping each field's newest entry until every field is down to one.
        for floor in (1, 0):
            for field in ("hints", "surfaces", "sources", "assets"):
                while len(json.dumps(value, ensure_ascii=False)) > self.context_budget and len(discovery[field]) > floor:
                    discovery[field].pop()
                    discovery["truncated"] = True
        if len(json.dumps(value, ensure_ascii=False)) > self.context_budget:
            raise V4BudgetStop("context_budget")
        return value

    @staticmethod
    def _discovery_evidence(node_id, state):
        """Expose evidence from the successful attempt that introduced this node."""
        node = state["nodes"][node_id]
        parents = {path["parent_id"] for path in node["discovery_paths"]}
        matches = []
        for parent_id in parents:
            for attempt_id in state["nodes"][parent_id]["attempts"]:
                attempt = state["attempts"][attempt_id]
                if attempt["status"] != "imported" or attempt["execution_status"] != "succeeded":
                    continue
                result = attempt["result"]
                discoveries = [item for item in result["discovered"] if dedup_key(item) == node["dedup_key"]]
                if discoveries:
                    refs, seen = [], set()
                    for discovery in discoveries:
                        for ref in discovery.get("evidence_refs", result["evidence_refs"]):
                            if ref["path"] not in seen:
                                refs.append(ref)
                                seen.add(ref["path"])
                    matches.append((attempt["imported_seq"], parent_id, attempt, refs))
        if not matches:
            return None
        matches.sort(key=lambda item: (item[0], item[1]), reverse=True)
        _, parent_id, attempt, refs = next((item for item in matches if item[3]), matches[0])
        result = attempt["result"]
        claim = result["claim"]
        return {"parent_id": parent_id, "attempt_id": attempt["attempt_id"], "claim": claim[:1000],
                "evidence_refs": [{"path": ref["path"], "description": ref["description"][:200]}
                                  for ref in refs[:8]],
                "truncated": len(claim) > 1000 or len(refs) > 8
                             or any(len(ref["description"]) > 200 for ref in refs)}

    def _discovery_context(self, node_id, state):
        """Summarize preserved same-origin observations; the model submits discoveries."""
        target = normalize_url(self.log.metadata["target"])
        origin = urlsplit(target)
        allowed = (origin.scheme, origin.hostname, origin.port or (443 if origin.scheme == "https" else 80))
        node = state["nodes"][node_id]
        refs, seen_refs = [], set()
        for attempt_id in node["attempts"]:
            attempt = state["attempts"][attempt_id]
            if attempt["status"] != "imported" or attempt["execution_status"] != "succeeded":
                continue
            for ref in attempt["evidence_refs"]:
                if ref.get("source") != "model_file" or "runtime_path" not in ref:
                    continue
                identity = (ref["sha256"], ref["runtime_path"])
                if identity not in seen_refs:
                    refs.append(ref)
                    seen_refs.add(identity)
        full = {"schema_version": 1, "node_id": node_id, "assets": [], "surfaces": [], "hints": [],
                "sources": [], "truncated": len(refs) > 16}
        seen_items = {key: set() for key in ("assets", "surfaces", "hints")}
        for ref in reversed(refs[-16:]):
            runtime_path = evidence_path(ref["runtime_path"])
            cache_key = (ref["sha256"], node["locator"]["url"], runtime_path,
                         runtime_path.lower().endswith((".js", ".mjs", ".cjs")))
            if cache_key not in self._discovery_cache:
                source_url, content_type, source_url_inferred = node["locator"]["url"], "", True
                try:
                    raw = _read_bounded(self.log.run_dir, ref["path"], MAX_EVIDENCE_BYTES)
                    if len(raw) != ref["bytes"] or hashlib.sha256(raw).hexdigest() != ref["sha256"]:
                        raise ValueError("Preserved evidence changed")
                    body = raw.decode("utf-8", "replace")
                    for line in body[:4096].splitlines()[:20]:
                        match = re.match(r"(?i)^\s*(?:URL\s*:\s*|(?:GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+)(https?://\S+)", line)
                        if match:
                            source_url = normalize_url(match.group(1))
                            source = urlsplit(source_url)
                            if (source.scheme, source.hostname, source.port or (443 if source.scheme == "https" else 80)) != allowed:
                                raise ValueError("Evidence source is outside the allowed origin")
                            source_url_inferred = False
                        match = re.match(r"(?i)^\s*Content-Type\s*:\s*(.+)", line)
                        if match:
                            content_type = match.group(1).strip()[:200]
                    if cache_key[-1] or urlsplit(source_url).path.lower().endswith((".js", ".mjs", ".cjs")):
                        content_type = "application/javascript"
                    from engine.discovery import analyze_response
                    analysis = analyze_response(body, source_url, content_type=content_type,
                                                allowed_origin=target, limit=100)
                    self._discovery_cache[cache_key] = {"source_url": source_url,
                        "source_url_inferred": source_url_inferred, "analysis": analysis}
                except (OSError, ValueError) as exc:
                    self._discovery_cache[cache_key] = {"source_url": source_url,
                        "source_url_inferred": source_url_inferred, "excluded_reason": str(exc)[:160],
                        "analysis": {"assets": [], "surfaces": [], "hints": [], "truncated": True}}
            cached = self._discovery_cache[cache_key]
            source = {"evidence_path": runtime_path, "source_url": cached["source_url"],
                      "source_url_inferred": cached["source_url_inferred"], "sha256": ref["sha256"]}
            if "excluded_reason" in cached:
                source["excluded_reason"] = cached["excluded_reason"]
            full["sources"].append(source)
            for field in seen_items:
                for item in cached["analysis"][field]:
                    identity = json.dumps(item, ensure_ascii=False, sort_keys=True)
                    if identity not in seen_items[field]:
                        full[field].append(item)
                        seen_items[field].add(identity)
            full["truncated"] = full["truncated"] or cached["analysis"]["truncated"]
        fetched = {}
        for source in full["sources"]:
            if not source["source_url_inferred"] and "excluded_reason" not in source:
                fetched.setdefault(source["source_url"], source["evidence_path"])
        names = {}
        for asset in full["assets"]:
            names.setdefault(urlsplit(asset["url"]).path.rsplit("/", 1)[-1].lower(), []).append(asset["url"])
        inferred = {}
        for source in full["sources"]:
            if source["source_url_inferred"] and "excluded_reason" not in source:
                matching = names.get(source["evidence_path"].rsplit("/", 1)[-1].lower(), [])
                if len(matching) == 1 and matching[0] not in fetched:
                    inferred.setdefault(matching[0], source["evidence_path"])
        full["assets"] = [{**asset, "fetched": asset["url"] in fetched or asset["url"] in inferred,
                           "fetched_inferred": asset["url"] in inferred,
                           **({"evidence_path": (fetched | inferred)[asset["url"]]}
                              if asset["url"] in fetched or asset["url"] in inferred else {})}
                          for asset in full["assets"]]
        relative = f"work/discovery/{node_id}.json"
        directory = _safe_path(self.runtime.runtime_dir, "work/discovery")
        directory.mkdir(parents=True, exist_ok=True)
        atomic_write_json(_safe_path(self.runtime.runtime_dir, relative), full)
        return {**{key: full[key][:20] for key in ("assets", "surfaces", "hints", "sources")},
                "truncated": full["truncated"] or any(len(full[key]) > 20 for key in seen_items),
                "full_analysis_path": relative}

    @staticmethod
    def _sanitize_result_observations(value):
        """Drop only invalid optional scorer features; never invent evidence or a verdict."""
        if not isinstance(value, dict):
            return value, []
        removed = []
        targets = [("result", value)]
        if isinstance(value.get("discovered"), list):
            targets.extend((f"discovered[{index}]", item) for index, item in enumerate(value["discovered"])
                           if isinstance(item, dict))
        for label, item in targets:
            if "observations" not in item:
                continue
            raw = item["observations"]
            if not isinstance(raw, dict):
                item.pop("observations")
                removed.append(label)
                continue
            accepted = {}
            for field, field_value in raw.items():
                if field not in OBSERVATIONS_SCHEMA["properties"]:
                    removed.append(f"{label}.{field}")
                    continue
                try:
                    accepted[field] = observations({field: field_value})[field]
                except ValueError:
                    removed.append(f"{label}.{field}")
            if accepted:
                item["observations"] = observations(accepted)
            else:
                item.pop("observations")
        return value, removed

    def _normalize_optional_result(self, attempt):
        """Keep valid discoveries while dropping only malformed optional metadata."""
        try:
            raw = _read_bounded(self.runtime.runtime_dir, attempt["result_path"], MAX_RESULT_BYTES)
            value = _json_result(raw)
        except (ValueError, OSError):
            return False
        if not isinstance(value, dict):
            return False
        value, removed = self._sanitize_result_observations(value)
        discoveries = value.get("discovered")
        for index, item in enumerate(discoveries if isinstance(discoveries, list) else []):
            locator = item.get("locator") if isinstance(item, dict) else None
            if not isinstance(locator, dict) or "route_template" not in locator:
                continue
            template = locator["route_template"]
            if (not isinstance(template, str) or not template.startswith("/") or template.startswith("//")
                    or len(template) > 2048 or any(char in template for char in "?#\\")):
                locator.pop("route_template")
                removed.append(f"discovered[{index}].locator.route_template")
        if not removed:
            return False
        try:
            normalized = validate_result(value, attempt_id=attempt["attempt_id"], node_id=attempt["node_id"])
        except ValueError:
            return False
        original_ref = self.log.preserve_bytes(f"invalid-node-results/{attempt['attempt_id']}-before-normalize.json", raw)
        atomic_write_json(_safe_path(self.runtime.runtime_dir, attempt["result_path"]), normalized)
        self.log.append("v4_result_optional_fields_dropped", {"attempt_id": attempt["attempt_id"],
            "node_id": attempt["node_id"], "fields": removed, "original_ref": original_ref})
        return True

    def _import_result(self, attempt, exec_id):
        try:
            return self.tree.import_result(attempt["attempt_id"], exec_id=exec_id)
        except (ValueError, OSError):
            if self._normalize_optional_result(attempt):
                return self.tree.import_result(attempt["attempt_id"], exec_id=exec_id)
            raise

    def _investigate(self, node_id):
        if not seed_dispatchable(self.tree.refresh(), node_id, self.log.metadata["target"]):
            raise ContractError("Seed node is outside scope or has unverified dependencies")
        unproductive = 0
        node_tools = 0
        complete, stop_reason = False, "node_tool_limit"
        unproductive_reason = "node_attempt_limit"
        self.tree.select_node(node_id)
        while True:
            if node_tools >= self.settings["node_tool_calls"]:
                stop_reason = "node_tool_limit"
                break
            if self.gate.check()["tool_calls"] >= self.gate.budgets["tool_calls"]:
                raise V4BudgetStop("call_budget")
            if unproductive >= self.settings["node_attempts"]:
                stop_reason = unproductive_reason
                break
            context = self._context(node_id, unproductive=unproductive, node_tools=node_tools)
            try:
                command = self.harness.decide(self.log.run_id, context, model=self.log.metadata["model"],
                    settings=self.log.metadata["model_settings"], deadline=self.gate.deadline,
                    command_validator=lambda candidate: self._validate_command(candidate, context["attempt"]["result_path"],
                        pending_followup=(context["previous_result"] or {}).get("next_observation", ""),
                        require_candidate_observation=(context["node"]["kind"] == "candidate"
                            and context["previous_result"] is None)))
            except V4BudgetStop as exc:
                if exc.reason == "node_limit":
                    self.log.append("v4_node_limit", {"node_id": node_id, "reason": "deep_calls_reserved"})
                    stop_reason = "deep_calls_reserved"
                    break
                raise
            except ModelFailure as exc:
                if exc.kind in ("budget", "cancelled", "timed_out"):
                    raise V4BudgetStop({"budget": "call_budget", "cancelled": "user_stopped", "timed_out": "time_budget"}[exc.kind])
                if exc.kind == "transport":
                    raise V4BudgetStop("model_unavailable")
                self.history.append({"node_id": node_id, "error": exc.kind})
                unproductive += 1
                unproductive_reason = "model_" + exc.kind
                continue
            if command.action == "finish":
                stop_reason = "no_successful_observation"
                break
            if self.gate.check()["tool_calls"] >= self.gate.budgets["tool_calls"]:
                raise V4BudgetStop("call_budget")
            executed = command.action in ("run_command", "run_python")
            attempt = None
            if executed:
                attempt = self.tree.begin_attempt(node_id, call_id=command.call_id)
                if attempt["attempt_id"] != context["attempt"]["attempt_id"]:
                    raise ContractError("Attempt identity changed while deciding")
                _safe_path(self.runtime.runtime_dir, attempt["result_path"]).parent.mkdir(parents=True, exist_ok=True)
            before = self.tree.state if attempt else None
            timeout = min(self.gate.budgets["command_seconds"], self.gate.deadline - self.gate.clock())
            if timeout <= 0:
                raise V4BudgetStop("time_budget")
            output = self.runtime.run(self.log.run_id, command.runtime_payload(), call_id=command.call_id, timeout=timeout)
            node_tools += 1
            self.history.append({"node_id": node_id, "action": command.action, "exit_code": output.exit_code,
                                 "path": command.path if command.action in ("read_file", "write_file") else "",
                                 "stdout": output.stdout[-2500:], "stderr": output.stderr[-1000:]})
            if getattr(output, "http_captures", None):
                self.history[-1]["http_captures"] = output.http_captures[-16:]
            if not attempt:
                continue
            try:
                imported = self._import_result(attempt, output.exec_id)
            except (ValueError, OSError) as exc:
                reason = "missing_result" if not (self.runtime.runtime_dir / attempt["result_path"]).exists() else "invalid_result"
                self.history[-1]["result_error"] = reason
                original_ref = None
                if reason == "invalid_result":
                    raw = _read_bounded(self.runtime.runtime_dir, attempt["result_path"], MAX_EVIDENCE_BYTES)
                    original_ref = self.log.preserve_bytes(f"invalid-node-results/{attempt['attempt_id']}-before-repair.json", raw)
                self.log.append("v4_result_repair_requested", {"attempt_id": attempt["attempt_id"],
                    "node_id": node_id, "exec_id": output.exec_id, "reason": reason, "original_ref": original_ref})
                if (node_tools >= self.settings["node_tool_calls"]
                        or self.gate.check()["tool_calls"] >= self.gate.budgets["tool_calls"]):
                    self.tree.fail_attempt(attempt["attempt_id"], reason=reason, exec_id=output.exec_id)
                    unproductive += 1
                    unproductive_reason = "node_attempt_limit"
                    continue
                repair = self._context(node_id, unproductive=unproductive, node_tools=node_tools)
                repair["attempt"] = {"attempt_id": attempt["attempt_id"], "node_id": node_id,
                                     "result_path": attempt["result_path"]}
                repair["result_template"]["attempt_id"] = attempt["attempt_id"]
                repair.pop("result_schema", None)
                contract_detail = (" Contract error: " + str(exc)[:240] + ".") if isinstance(exc, ContractError) else ""
                repair["instruction"] = ("The preceding tool already ran for this attempt but its result JSON is "
                    + reason + "." + contract_detail + " Use action=write_file, path=attempt.result_path, and content as a JSON string "
                    "made from result_template. Include specific discovered input surfaces if observed. "
                    "Keep every template field. evidence_refs may refer only to separately written observation files, "
                    "never a node-results JSON or an absolute path. Do not run another target request in this response.")
                def validate_correction(candidate):
                    expected = attempt["result_path"]
                    try:
                        same_file = (candidate.action == "write_file"
                                     and _safe_path(self.runtime.runtime_dir, candidate.path)
                                     == _safe_path(self.runtime.runtime_dir, expected))
                    except (ValueError, OSError):
                        same_file = False
                    if not same_file:
                        raise ValueError(f"Use action=write_file with path exactly {expected!r} and no other action")
                    value = json.loads(candidate.content)
                    if isinstance(value, dict) and "next_observation" not in value:
                        raise ValueError("Result JSON is missing next_observation")
                    value, removed = self._sanitize_result_observations(value)
                    validate_result(value, attempt_id=attempt["attempt_id"], node_id=node_id)
                    if removed:
                        self.log.append("v4_result_observations_dropped", {"attempt_id": attempt["attempt_id"],
                            "fields": removed})
                    return replace(candidate, content=json.dumps(value, ensure_ascii=False))

                try:
                    correction = self.harness.decide(self.log.run_id, repair, model=self.log.metadata["model"],
                        settings=self.log.metadata["model_settings"], deadline=self.gate.deadline,
                        command_validator=validate_correction)
                except V4BudgetStop:
                    self.tree.fail_attempt(attempt["attempt_id"], reason=reason, exec_id=output.exec_id)
                    raise
                except ModelFailure as model_exc:
                    self.tree.fail_attempt(attempt["attempt_id"], reason=reason, exec_id=output.exec_id)
                    if model_exc.kind in ("budget", "cancelled", "timed_out", "transport"):
                        raise V4BudgetStop({"budget": "call_budget", "cancelled": "user_stopped",
                            "timed_out": "time_budget", "transport": "model_unavailable"}[model_exc.kind]) from model_exc
                    unproductive += 1
                    unproductive_reason = "node_attempt_limit"
                    continue
                self.gate.check()
                fix_timeout = min(self.gate.budgets["command_seconds"], self.gate.deadline - self.gate.clock())
                if fix_timeout <= 0:
                    self.tree.fail_attempt(attempt["attempt_id"], reason=reason, exec_id=output.exec_id)
                    raise V4BudgetStop("time_budget")
                fixed = self.runtime.run(self.log.run_id, correction.runtime_payload(), call_id=correction.call_id,
                                         timeout=fix_timeout)
                node_tools += 1
                self.log.append("v4_result_repair_finished", {"attempt_id": attempt["attempt_id"],
                    "call_id": correction.call_id, "exec_id": fixed.exec_id, "exit_code": fixed.exit_code})
                if fixed.exit_code != 0 or fixed.timed_out:
                    self.tree.fail_attempt(attempt["attempt_id"], reason=reason, exec_id=output.exec_id)
                    unproductive += 1
                    unproductive_reason = "node_attempt_limit"
                    continue
                try:
                    imported = self._import_result(attempt, output.exec_id)
                except (ValueError, OSError):
                    self.tree.fail_attempt(attempt["attempt_id"], reason="invalid_result", exec_id=output.exec_id)
                    unproductive += 1
                    unproductive_reason = "node_attempt_limit"
                    continue
            if imported["execution_status"] != "succeeded":
                unproductive += 1
                unproductive_reason = "node_attempt_limit"
                continue
            origin = urlsplit(self.log.metadata["target"])
            allowed = (origin.scheme, origin.hostname, origin.port or (443 if origin.scheme == "https" else 80))
            for discovered in self.tree.state["nodes"].values():
                location = urlsplit(discovered["locator"]["url"])
                actual = (location.scheme, location.hostname, location.port or (443 if location.scheme == "https" else 80))
                if actual != allowed and discovered["exploration_state"] not in ("done", "blocked"):
                    self.tree.set_node_state(discovered["id"], "blocked")
                    self.log.append("v4_scope_excluded", {"node_id": discovered["id"]})
            progressed = self._made_progress(node_id, imported, before)
            if progressed:
                unproductive, node_tools = 0, 0
            else:
                unproductive += 1
                unproductive_reason = "no_progress_limit"
            self.log.append("v4_node_progress", {"node_id": node_id, "attempt_id": attempt["attempt_id"],
                "progressed": progressed, "unproductive_results": unproductive, "tools_since_progress": node_tools})
            if self.tree.state["nodes"][node_id].get("origin") == "unverified_lead":
                complete, stop_reason = True, "verification_attempted"
                break
            if imported["result"]["next_observation"].strip():
                continue
            complete, stop_reason = True, "observations_complete"
            break
        state = self.tree.state
        if complete and node_id == state["root_id"] and not any(
                key != node_id and node["exploration_state"] != "blocked" for key, node in state["nodes"].items()):
            complete, stop_reason = False, "no_actionable_surfaces"
        self.tree.finish_investigation(node_id, status="done" if complete else "incomplete", reason=stop_reason)

    def _made_progress(self, node_id, imported, before):
        if any(key not in before["nodes"] and node["exploration_state"] != "blocked"
               for key, node in self.tree.state["nodes"].items()):
            return True
        seen = {ref["sha256"] for ref in before["nodes"][node_id]["evidence_refs"] if ref.get("source") == "model_file"}
        if any(ref["sha256"] not in seen for ref in imported["evidence_refs"]):
            return True
        known_observations = [observations({})]
        for attempt_id in before["nodes"][node_id]["attempts"]:
            attempt = before["attempts"][attempt_id]
            if attempt["status"] == "imported" and attempt["execution_status"] == "succeeded":
                known_observations.append(attempt.get("result", {}).get("observations"))
        current = imported["result"].get("observations")
        return current is not None and current not in known_observations

    @staticmethod
    def _validate_command(command, result_path, *, pending_followup="", require_candidate_observation=False):
        if command.action == "finish" and pending_followup.strip():
            raise ValueError("A follow-up observation is pending. Investigate previous_result.next_observation and write a new result before finish")
        if command.action == "finish" and require_candidate_observation:
            raise ValueError("Candidate has no successful observation. Run a controlled test and write its result before finish; HTTP 500 alone does not confirm the hypothesis")
        if command.action == "run_python":
            problems = check_generated_code(command.code)
            if problems:
                raise ValueError("; ".join(problems[:3]))
        if command.action == "run_python" and result_path not in command.code:
            raise ValueError("run_python must write result JSON to attempt.result_path before it exits")
        return command

    def run(self):
        try:
            self._eager_seed_candidates()
            current = self.tree.state["root_id"]
            deferred = set()
            dependency_version = None
            while current or deferred:
                if current is None:
                    if not self._wait_for_seed_review():
                        return "incomplete", "seed_dependencies_deferred"
                    deferred.clear()
                    dependency_version = None
                    current = self.tree.refresh()["root_id"]
                self.gate.check()
                state = self.tree.refresh()
                # Operator-reviewed edges and stale propagation may change
                # while another branch is being investigated. A previously
                # deferred parent must then be reconsidered.
                current_version = state["seed_dependency_revision"]
                if dependency_version is not None and current_version != dependency_version:
                    deferred.clear()
                dependency_version = current_version
                if needs_seed_prerequisite_observation(state, current):
                    self.tree.request_seed_reinvestigation(current)
                node = self.tree.refresh()["nodes"][current]
                if node["investigation_state"] == "pending":
                    self._investigate(current)
                node = self.tree.refresh()["nodes"][current]
                pending = [key for key in node["child_ids"] if self.tree.state["nodes"][key]["exploration_state"] not in ("done", "blocked")]
                if pending:
                    eligible = [key for key in pending if key not in deferred and seed_dispatchable(self.tree.state, key, self.log.metadata["target"])]
                    if not eligible:
                        self.log.append("v4_seed_dependencies_deferred", {"parent_id": current, "node_ids": pending})
                        deferred.add(current)
                        current = node["parent_id"]
                        continue
                    ranked = self.scorer.score_children(current, include_completed=True, reserve_final=True)
                    if not ranked["ready"]:
                        if ranked["stop_reason"] == "context_changed":
                            continue
                        raise V4BudgetStop(ranked["stop_reason"])
                    selected = next((key for key in ranked["ordered_node_ids"] if key in eligible), None)
                    if selected is None:
                        return "incomplete", "seed_dependencies_deferred"
                    self.log.append("v4_node_selection", {"parent_id": current, "node_id": selected,
                        "score": ranked["scores"].get(selected), "degraded_mode": selected in ranked["unscored"]})
                    current = selected
                else:
                    # Leaf observations can invalidate their own prior score. A
                    # final sibling assessment includes completed nodes without
                    # putting them back into the exploration queue.
                    if any(self.tree.state["nodes"][key]["exploration_state"] != "blocked"
                           and self.tree.state["nodes"][key]["risk"]["status"] in ("pending", "stale", "unscored")
                           for key in node["child_ids"]):
                        ranked = self.scorer.score_children(current, include_completed=True)
                        if not ranked["ready"]:
                            if ranked["stop_reason"] == "context_changed":
                                continue
                            raise V4BudgetStop(ranked["stop_reason"])
                    parent = node["parent_id"]
                    self.tree.set_node_state(current, "done")
                    current = parent
            if investigation_gaps(self.tree.refresh()):
                return "incomplete", "investigation_incomplete"
            return "completed", "tree_exhausted"
        except V4BudgetStop as exc:
            outcome = "stopped" if exc.reason == "user_stopped" else "timed_out" if exc.reason == "time_budget" else "budget_exhausted" if exc.reason in ("call_budget", "cost_budget") else "error"
            return outcome, exc.reason
        finally:
            try:
                usage = self.gate.usage()
                self.log.append("v4_exploration_finished", {"elapsed_seconds": usage["elapsed_seconds"]})
                self.log.update(v4_usage=usage)
            finally:
                self.scorer.close()

    def _reviewable_seed_dependencies(self, state):
        """List unverified edges with fresh, reviewable evidence in this run."""
        pending = []
        for child in state["nodes"].values():
            if (not child.get("seeded") or child["exploration_state"] in ("done", "blocked")
                    or seed_dispatchable(state, child["id"], self.log.metadata["target"])):
                continue
            verified = child.get("seed_enabled_by_verified", {})
            for path in child["discovery_paths"]:
                parent_id = path["parent_id"]
                if (path["relation"] != "enabled_by" or parent_id in verified
                        or parent_id not in state["nodes"]
                        or not seed_dispatchable(state, parent_id, self.log.metadata["target"])):
                    continue
                for attempt_id in reversed(state["nodes"][parent_id]["attempts"]):
                    attempt = state["attempts"][attempt_id]
                    if (attempt["status"] != "imported" or attempt["execution_status"] != "succeeded"
                            or attempt["imported_seq"] <= seed_dependency_invalidation_seq(child, parent_id)):
                        continue
                    refs = [ref for ref in attempt["evidence_refs"] if ref.get("source") == "model_file"]
                    if refs:
                        pending.append({"child_id": child["id"], "parent_id": parent_id,
                                        "attempt_id": attempt_id, "evidence_refs": refs})
                        break
        return pending

    def _wait_for_seed_review(self):
        """Wait briefly for a review event, without issuing calls or losing stop requests."""
        wake = threading.Event()
        def on_event(event):
            if event.get("kind") == "node_seed_dependency_verified":
                wake.set()
        self.log.listen(on_event)
        try:
            state = self.tree.refresh()
            pending = self._reviewable_seed_dependencies(state)
            remaining = min(self.seed_review_wait_seconds, max(0.0, self.gate.deadline - self.gate.clock()))
            if not pending or remaining <= 0:
                return False
            self.log.append("v4_seed_review_waiting", {"dependencies": pending, "timeout_seconds": remaining})
            deadline = time.monotonic() + remaining
            while True:
                if self.stop_event.is_set():
                    raise V4BudgetStop("user_stopped")
                state = self.tree.refresh()
                if any(seed_dispatchable(state, item["child_id"], self.log.metadata["target"]) for item in pending):
                    return True
                left = min(deadline - time.monotonic(), self.gate.deadline - self.gate.clock())
                if left <= 0:
                    return False
                wake.wait(min(.2, left))
                wake.clear()
        finally:
            self.log.unlisten(on_event)

    def _eager_seed_candidates(self):
        settings = self.log.metadata.get("seed_settings") or {}
        if settings.get("start_mode") != "seeded" or settings.get("reverify") != "eager":
            return
        start = self.gate.usage()["model_calls"]
        allowance = self.gate.budgets["model_calls"] // 4
        if not allowance:
            return
        self.gate.eager_call_limit = start + allowance
        try:
            state = self.tree.refresh()
            candidates = [node for node in state["nodes"].values()
                          if node.get("seeded") and node["kind"] == "candidate"
                          and node["investigation_state"] == "pending"]
            candidates.sort(key=lambda node: (node["risk"].get("score") is None,
                                              -(node["risk"].get("score") or 0), node["id"]))
            for node in candidates[:5]:
                self._observe_eager_prerequisites(node["id"])
                state = self.tree.refresh()
                current = state["nodes"][node["id"]]
                if current["exploration_state"] in ("done", "blocked") or current["investigation_state"] != "pending":
                    continue
                if not seed_dispatchable(state, node["id"], self.log.metadata["target"]):
                    continue
                self._investigate(node["id"])
        except V4BudgetStop as exc:
            if exc.reason != "eager_limit":
                raise
            self.log.append("v4_seed_eager_limit", {"model_call_limit": allowance})
        finally:
            self.gate.eager_call_limit = None

    def _observe_eager_prerequisites(self, node_id):
        """Observe reachable primary and alternate prerequisite parents within the eager allowance."""
        state = self.tree.refresh()
        dependencies, visited, scheduled = [], set(), set()

        def collect(current_id):
            if current_id in visited or current_id not in state["nodes"]:
                return
            visited.add(current_id)
            node = state["nodes"][current_id]
            primary_parent = node.get("parent_id")
            for path in node.get("discovery_paths", []):
                parent_id = path["parent_id"]
                if parent_id != primary_parent and path["relation"] != "enabled_by":
                    continue
                collect(parent_id)
                if (path["relation"] == "enabled_by" and node.get("seeded")
                        and parent_id not in scheduled):
                    dependencies.append(parent_id)
                    scheduled.add(parent_id)

        collect(node_id)
        for parent_id in dependencies:
            state = self.tree.refresh()
            parent = state["nodes"].get(parent_id)
            if parent is None:
                continue
            if parent["exploration_state"] in ("done", "blocked"):
                continue
            if needs_seed_prerequisite_observation(state, parent_id):
                self.tree.request_seed_reinvestigation(parent_id)
                parent = self.tree.refresh()["nodes"][parent_id]
            if parent["investigation_state"] == "pending" and seed_dispatchable(self.tree.state, parent_id, self.log.metadata["target"]):
                self._investigate(parent_id)

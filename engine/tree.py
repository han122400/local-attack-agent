"""Event-sourced v4 foundation. No model, browser, HTTP or command execution.

Events are authoritative. Snapshots are replaceable indexes, never a reason to
reissue a command. Result files are captured before their import is committed.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import stat
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from engine.session_log import LogFailure, SessionLog, atomic_write_json, safe_relative_path, validate_run_id
from engine.contracts import (SCHEMA_VERSION, MAX_RESULT_BYTES, MAX_EVIDENCE_BYTES, MAX_TOTAL_EVIDENCE_BYTES,
                          ContractError, dedup_key, normalize_url, validate_discovery, validate_id, validate_result)
from engine.risk_rubric import observations
from engine.observation_verification import POLICIES, verify_observations
from engine.followup import (UNVERIFIED_LEAD_HYPOTHESIS_TYPES, UNVERIFIED_LEAD_LIMIT, UNVERIFIED_LEAD_POLICY,
                             followup_candidates, unverified_lead_candidates)
from engine.scorer_state import SCORING_EVENTS, apply_scoring_event, empty_scorer, invalidate_risk, usage as scorer_usage


_KINDS = {"tree_initialized", "node_discovered", "node_seeded", "node_seed_freshness_recorded", "node_seed_reinvestigation_requested", "node_seed_dependency_verified", "node_selected", "node_state_changed",
          "node_attempt_started", "node_result_imported", "node_observations_changed", "node_attempt_failed",
          "node_verification_recorded", "node_investigation_finished", "http_exchange_recorded", *SCORING_EVENTS}

MAX_UNVERIFIED_LEADS_PER_RUN = UNVERIFIED_LEAD_LIMIT


def _under_unverified_lead(state: dict, node: dict) -> bool:
    while node is not None:
        if node.get("origin") == "unverified_lead":
            return True
        node = state["nodes"].get(node["parent_id"]) if node.get("parent_id") else None
    return False


def _empty_state(run_id: str) -> dict:
    # The dependency revision is the last event sequence that changed dependency state.
    return {"schema_version": SCHEMA_VERSION, "run_id": validate_run_id(run_id), "root_id": None,
            "current_id": None, "last_event_seq": 0, "seed_dependency_revision": 0,
            "nodes": {}, "attempts": {}, "executions": {}, "scorer": empty_scorer()}


def _digest(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode("utf-8")).hexdigest()


def _node(node_id: str, parent_id: str | None, discovery: dict) -> dict:
    return {"schema_version": SCHEMA_VERSION, "id": node_id, "parent_id": parent_id, "child_ids": [],
            "kind": discovery["kind"], "locator": discovery["locator"], "hypothesis": discovery["hypothesis"],
            "dedup_key": dedup_key(discovery), "discovery_paths": [] if parent_id is None else [
                {"parent_id": parent_id, "relation": discovery["relation"]}],
            "exploration_state": "discovered", "verification_state": "untested",
            "investigation_state": "pending", "investigation_reason": None,
            "observations": observations(discovery.get("observations", {})),
            "risk": {"status": "not_applicable" if parent_id is None else "pending", "score": None},
            "attempts": [], "evidence_refs": [], "verification": None}


def latest_observation(state: dict, node_id: str) -> dict | None:
    for attempt_id in reversed(state["nodes"][node_id]["attempts"]):
        attempt = state["attempts"][attempt_id]
        if attempt["status"] == "imported" and attempt["execution_status"] == "succeeded":
            return attempt.get("result")
    return None


def investigation_gaps(state: dict) -> list[dict]:
    nodes = [node for node in state["nodes"].values() if node["exploration_state"] != "blocked"]
    gaps = []
    for node in nodes:
        previous = latest_observation(state, node["id"])
        followup = previous["next_observation"].strip() if previous else ""
        if node.get("seed_revalidation_pending"):
            reason = "seed_revalidation_pending"
        elif node["investigation_state"] == "reused" and node.get("seeded") and node["kind"] in ("root", "surface"):
            if node["id"] == state["root_id"] and len(nodes) == 1:
                reason = "no_actionable_surfaces"
            else:
                continue
        elif node["investigation_state"] != "done":
            reason = node.get("investigation_reason") or "investigation_pending"
        elif node.get("origin") == "unverified_lead":
            # A verification lead is deliberately investigated once. A model's
            # remaining suggestion cannot requeue it after that investigation.
            continue
        elif previous is None:
            reason = "no_successful_observation"
        elif followup:
            reason = "followup_pending"
        elif node["id"] == state["root_id"] and len(nodes) == 1:
            reason = "no_actionable_surfaces"
        else:
            continue
        gaps.append({"node_id": node["id"], "reason": reason, "next_observation": followup})
    return gaps


def _invalidate_children(state: dict, parent_id: str | None) -> None:
    if parent_id is not None:
        for child_id in state["nodes"][parent_id]["child_ids"]:
            node = state["nodes"][child_id]
            if node["exploration_state"] != "blocked":
                invalidate_risk(node)


def seed_dependency_invalidation_seq(node: dict, parent_id: str) -> int:
    """Keep legacy journals' node-wide cutoff, then use per-edge cutoffs."""
    cutoffs = node.get("seed_dependency_invalidated_seqs")
    return cutoffs.get(parent_id, 0) if cutoffs is not None else node.get("seed_dependency_invalidated_seq", 0)


def _has_independent_seed_path(state: dict, node_id: str, excluded: set[str]) -> bool:
    """Match execution paths while excluding assumptions changed in this event."""
    def visit(current_id, visiting, provenance_only=False):
        if ((current_id in excluded and not provenance_only) or current_id in visiting
                or current_id not in state["nodes"]):
            return False
        node = state["nodes"][current_id]
        if node["exploration_state"] == "blocked":
            return False
        parent_id = node["parent_id"]
        if parent_id is None:
            return current_id == state["root_id"]
        primary = next((path for path in node["discovery_paths"] if path["parent_id"] == parent_id), None)
        if primary is None:
            return False
        verified = node.get("seed_enabled_by_verified", {})
        paths = [primary, *(path for path in node["discovery_paths"]
                            if path != primary and path["relation"] == "enabled_by" and path["parent_id"] in verified)]
        for path in paths:
            if path["relation"] == "enabled_by" and node.get("seeded") and path["parent_id"] not in verified:
                continue
            if visit(path["parent_id"], visiting | {current_id}, path["relation"] == "discovered"):
                return True
        return False
    return visit(node_id, set())


def _invalidate_seed_dependencies(state: dict, node_id: str, invalidation_seq: int) -> None:
    """Invalidate affected paths; preserve evidence supported by an independent path."""
    queue, invalidated, visited_edges = [node_id], {node_id}, set()
    while queue:
        parent = queue.pop()
        for child in state["nodes"].values():
            edge = (parent, child["id"])
            if edge in visited_edges or not any(
                path["parent_id"] == parent and path["relation"] in ("refines", "enabled_by")
                for path in child["discovery_paths"]
            ):
                continue
            visited_edges.add(edge)
            if child.get("seeded"):
                if "seed_dependency_invalidated_seqs" not in child:
                    legacy_cutoff = child.get("seed_dependency_invalidated_seq", 0)
                    child["seed_dependency_invalidated_seqs"] = {path["parent_id"]: legacy_cutoff
                                                                for path in child["discovery_paths"]}
                child["seed_dependency_invalidated_seqs"][parent] = invalidation_seq
                verified = child.get("seed_enabled_by_verified", {})
                if parent in verified:
                    verified.pop(parent)
                    state["seed_dependency_revision"] = invalidation_seq
            if child["id"] in invalidated or _has_independent_seed_path(state, child["id"], invalidated):
                continue
            invalidated.add(child["id"])
            queue.append(child["id"])
            if child.get("seeded"):
                if child.get("seed_freshness") != "unknown" or child.get("seed_revalidation_pending") is not True:
                    state["seed_dependency_revision"] = invalidation_seq
                child["seed_freshness"] = "unknown"
                child["seed_checked_fields"] = []
                child["seed_dependency_invalidated_seq"] = invalidation_seq
                child["seed_revalidation_pending"] = True
                child["observations"] = observations({})
                child.pop("observation_verification", None)
                if child["exploration_state"] not in ("done", "blocked"):
                    invalidate_risk(child)
                    child["investigation_state"] = "pending"
                    child["investigation_reason"] = "seed_dependency_changed"


def _response_status_evidence(data: bytes, target: str, method: str = "GET", auth_context: str = "anonymous") -> int | None:
    """Extract an exact URL/status pair from a preserved response header."""
    try:
        header = data[:8192].decode("utf-8").split("\n\n", 1)[0].split("\r\n\r\n", 1)[0]
        fields = {}
        for line in header.splitlines():
            if ":" not in line:
                continue
            name, value = line.split(":", 1)
            name = name.strip().lower()
            if name in ("url", "status", "method", "auth-context"):
                if name in fields:
                    return None
                fields[name] = value.strip()
        if (normalize_url(fields["url"]) != normalize_url(target) or not re.fullmatch(r"[1-5][0-9]{2}", fields["status"])
                or fields.get("method", "GET").upper() != method
                or fields.get("auth-context", "anonymous") != auth_context):
            return None
        return int(fields["status"])
    except (UnicodeError, ValueError, KeyError):
        return None


def _invalidate_failed_seed_attempt(state: dict, node: dict, seq: int) -> None:
    if node.pop("observation_verification", None) is not None:
        _invalidate_children(state, node["parent_id"])
        _invalidate_children(state, node["id"])
    if node.get("seeded"):
        _invalidate_seed_dependencies(state, node["id"], seq)
        if node.get("seed_freshness") != "unknown":
            state["seed_dependency_revision"] = seq
        node["seed_freshness"] = "unknown"
        node["seed_checked_fields"] = []


def _apply_seed_freshness(state: dict, payload: dict, seq: int) -> None:
    node = state["nodes"][validate_id(payload["node_id"])]
    attempt = state["attempts"][validate_id(payload["attempt_id"], attempt=True)]
    current = payload["response_status"]
    prior = node.get("seed_prior_observations", {}).get("response_status")
    ref = payload["evidence_ref"]
    if (not node.get("seeded") or node["locator"]["method"] != "GET"
            or node["locator"]["auth_context"] != "anonymous" or type(prior) is not int
            or type(current) is not int or not 100 <= current <= 599
            or attempt["node_id"] != node["id"] or attempt["status"] != "imported"
            or attempt["execution_status"] != "succeeded" or ref not in attempt["evidence_refs"]):
        raise ContractError("Invalid seed freshness evidence")
    previous = node.get("seed_freshness_evidence", {}).get("response_status")
    freshness = "checked" if current == prior else "stale"
    if node.get("seed_freshness") != freshness or node.get("seed_revalidation_pending") is not False:
        state["seed_dependency_revision"] = seq
    node["seed_freshness"] = freshness
    node["seed_checked_fields"] = ["response_status"]
    node["seed_freshness_evidence"] = {"attempt_id": attempt["attempt_id"], "evidence_ref": ref,
                                       "response_status": current}
    node["seed_revalidation_pending"] = False
    if current != prior:
        node["observations"] = observations({"response_status": current})
    else:
        node["observations"] = {**node["observations"], "response_status": current}
    if (previous is None and current != prior) or (previous is not None and previous != current):
        _invalidate_children(state, node["parent_id"])
        _invalidate_children(state, node["id"])
        _invalidate_seed_dependencies(state, node["id"], seq)


def _omit_redundant_route_template(discovery: dict) -> None:
    """Canonicalize new events without changing replay of persisted discoveries."""
    locator = discovery["locator"]
    if locator.get("route_template") == urlsplit(locator["url"]).path:
        locator.pop("route_template")


def _discover(state: dict, parent_id: str, discovery: dict) -> str:
    parent = state["nodes"][parent_id]
    if parent["exploration_state"] in ("done", "blocked"):
        raise ContractError("Terminal parents cannot acquire discoveries")
    discovery = validate_discovery(discovery)
    key = dedup_key(discovery)
    link = {"parent_id": parent_id, "relation": discovery["relation"]}
    for existing in state["nodes"].values():
        if existing["dedup_key"] == key:
            if link not in existing["discovery_paths"]:
                existing["discovery_paths"].append(link)
                state["seed_dependency_revision"] = state["last_event_seq"] + 1
            if "observations" in discovery and existing["observations"] != discovery["observations"]:
                existing["observations"] = discovery["observations"]
                existing.pop("observation_verification", None)
                _invalidate_children(state, existing["parent_id"])
                _invalidate_children(state, existing["id"])
            return existing["id"]
    node_id = f"N{len(state['nodes']) + 1:04d}"
    state["nodes"][node_id] = _node(node_id, parent_id, discovery)
    state["seed_dependency_revision"] = state["last_event_seq"] + 1
    parent["child_ids"].append(node_id)
    _invalidate_children(state, parent_id)
    return node_id


def _artifact_ref(value: dict) -> dict:
    if not isinstance(value, dict) or not {"path", "bytes", "sha256"} <= value.keys():
        raise ContractError("Invalid preserved evidence reference")
    relative = safe_relative_path(value["path"])
    if (len(relative.parts) < 2 or relative.parts[0] != "artifacts" or type(value["bytes"]) is not int
            or not 0 <= value["bytes"] <= MAX_EVIDENCE_BYTES or not isinstance(value["sha256"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", value["sha256"])):
        raise ContractError("Invalid preserved evidence reference")
    return copy.deepcopy(value)


def _execution(state: dict, attempt: dict, exec_id: str, *, interrupted: bool = False) -> str:
    execution = state["executions"].get(exec_id)
    if (execution is None or execution["call_id"] != attempt["call_id"]
            or execution["started_seq"] <= attempt["started_seq"]):
        raise ContractError("Execution does not belong to this attempt")
    finish = execution.get("finish")
    if finish is None:
        if interrupted:
            return "interrupted"
        raise ContractError("Execution has not finished")
    return "succeeded" if (type(finish.get("exit_code")) is int and finish["exit_code"] == 0
                            and not finish.get("timed_out") and not finish.get("cancelled")
                            and not finish.get("failure_source")) else "failed"


def _apply(state: dict, event: dict) -> None:
    if (event.get("run_id") != state["run_id"] or type(event.get("seq")) is not int
            or event["seq"] != state["last_event_seq"] + 1):
        raise ContractError("Event identity or sequence mismatch")
    kind = event.get("kind")
    if not isinstance(kind, str):
        raise ContractError("Missing event kind")
    if kind == "exec_started":
        exec_id = event.get("exec_id")
        if (not isinstance(exec_id, str) or not exec_id or exec_id in state["executions"]
                or not isinstance(event.get("action"), str) or not event["action"]):
            raise ContractError("Duplicate or invalid execution ID")
        state["executions"][exec_id] = {"call_id": event.get("call_id"), "action": event["action"],
                                       "started_seq": event["seq"]}
    elif kind == "exec_finished":
        execution = state["executions"].get(event.get("exec_id"))
        if execution is None or execution["call_id"] != event.get("call_id") or "finish" in execution:
            raise ContractError("Unmatched execution result")
        if (event.get("action") != execution["action"] or "exit_code" not in event
                or type(event["exit_code"]) not in (int, type(None))
                or type(event.get("timed_out")) is not bool or type(event.get("cancelled")) is not bool
                or "failure_source" not in event or type(event["failure_source"]) not in (str, type(None))):
            raise ContractError("Malformed execution result provenance")
        execution["finish"] = event
    if kind not in _KINDS:
        if kind.startswith(("tree_", "node_", "scorer_")):
            raise ContractError("Unsupported tree event")
        state["last_event_seq"] = event["seq"]
        return
    if type(event.get("tree_schema")) is not int or event["tree_schema"] != SCHEMA_VERSION:
        raise ContractError("Unsupported tree event schema")
    payload = event["payload"]
    if kind == "tree_initialized":
        if state["root_id"] is not None:
            raise ContractError("Tree already initialized")
        discovery = {"kind": "root", "relation": "discovered", "hypothesis": None,
                     "locator": {"url": normalize_url(payload["target"]), "method": "GET", "input": "",
                                 "auth_context": "anonymous"}}
        state["root_id"] = state["current_id"] = "N0001"
        state["nodes"]["N0001"] = _node("N0001", None, discovery)
        state["seed_dependency_revision"] = event["seq"]
    elif state["root_id"] is None:
        raise ContractError("Tree has not been initialized")
    elif kind in SCORING_EVENTS:
        apply_scoring_event(state, kind, payload)
    elif kind == "http_exchange_recorded":
        attempt = state["attempts"][payload["attempt_id"]]
        execution = state["executions"].get(event.get("exec_id"))
        facts_schema = payload.get("facts_schema", 1)
        if (attempt["status"] != "pending" or payload["node_id"] != attempt["node_id"]
                or event.get("call_id") != attempt["call_id"] or execution is None
                or execution["call_id"] != attempt["call_id"] or "finish" in execution
                or not re.fullmatch(r"R[0-9a-f]{32}", payload["request_id"])
                or type(facts_schema) is not int or facts_schema not in (1, 2)):
            raise ContractError("Invalid HTTP capture attribution")
        captures = state.setdefault("http_exchanges", {})
        if payload["request_id"] in captures:
            raise ContractError("Duplicate HTTP capture identity")
        for side in ("request", "response"):
            ref = _artifact_ref(payload[side])
            if ref.get("source") != "harness_http":
                raise ContractError("Invalid HTTP capture source")
            state["nodes"][attempt["node_id"]]["evidence_refs"].append(ref)
        captures[payload["request_id"]] = {**copy.deepcopy(payload), "exec_id": event["exec_id"]}
    elif kind == "node_observations_changed":
        node = state["nodes"][payload["node_id"]]
        value = observations(payload["observations"])
        if value != node["observations"]:
            node["observations"] = value
            node.pop("observation_verification", None)
            _invalidate_children(state, node["parent_id"])
            _invalidate_children(state, node["id"])
    elif kind == "node_discovered":
        _discover(state, payload["parent_id"], payload["discovery"])
    elif kind == "node_seeded":
        node = state["nodes"][validate_id(payload["node_id"])]
        source_run = validate_run_id(payload["source_run_id"])
        source_node = validate_id(payload["source_node_id"])
        reused = payload["reused"]
        if type(reused) is not bool or (reused and node["kind"] not in ("root", "surface")) or node.get("seeded"):
            raise ContractError("Invalid seed node")
        prior = observations(payload["observations"])
        node.update(seeded=True, carried_from={"run_id": source_run, "orig_node_id": source_node},
                    seed_prior_observations=prior, seed_freshness="unknown", observations=prior)
        state["seed_dependency_revision"] = event["seq"]
        if reused:
            node["investigation_state"] = "reused"
    elif kind == "node_seed_reinvestigation_requested":
        node = state["nodes"][validate_id(payload["node_id"])]
        if (not node.get("seeded") or node["investigation_state"] != "reused"
                or node["exploration_state"] in ("done", "blocked")):
            raise ContractError("Seed node cannot be reinvestigated")
        node["investigation_state"] = "pending"
        node["investigation_reason"] = "seed_reinvestigation_requested"
    elif kind == "node_seed_dependency_verified":
        child = state["nodes"][validate_id(payload["child_id"])]
        parent_id = validate_id(payload["parent_id"])
        parent = state["nodes"][parent_id]
        attempt = state["attempts"][validate_id(payload["attempt_id"], attempt=True)]
        evidence = payload["evidence_ref"]
        if (not child.get("seeded") or child["exploration_state"] == "blocked"
                or {"parent_id": parent_id, "relation": "enabled_by"} not in child["discovery_paths"]
                or parent["exploration_state"] == "blocked" or attempt["node_id"] != parent_id
                or attempt["status"] != "imported" or attempt["execution_status"] != "succeeded"
                or attempt["imported_seq"] <= seed_dependency_invalidation_seq(child, parent_id)
                or not isinstance(evidence, dict) or evidence.get("source") != "model_file"
                or evidence not in attempt["evidence_refs"]):
            raise ContractError("Invalid operator-reviewed dependency evidence")
        _artifact_ref(evidence)
        approval = {"source": "operator_reviewed", "attempt_id": attempt["attempt_id"],
                    "evidence_ref": copy.deepcopy(evidence)}
        verified = child.setdefault("seed_enabled_by_verified", {})
        if verified.get(parent_id) != approval:
            state["seed_dependency_revision"] = event["seq"]
        verified[parent_id] = approval
    elif kind == "node_seed_freshness_recorded":
        _apply_seed_freshness(state, payload, event["seq"])
    elif kind == "node_selected":
        node = state["nodes"][payload["node_id"]]
        if node["exploration_state"] in ("done", "blocked"):
            raise ContractError("Completed or blocked nodes cannot be selected")
        node["exploration_state"] = "active"
        state["current_id"] = node["id"]
    elif kind == "node_investigation_finished":
        status, reason = payload.get("status", "done"), payload.get("reason")
        if (status not in ("done", "incomplete")
                or (reason is not None and (not isinstance(reason, str) or not re.fullmatch(r"[a-z_]{1,128}", reason)))
                or (status == "incomplete" and reason is None)):
            raise ContractError("Invalid investigation outcome")
        state["nodes"][payload["node_id"]].update(investigation_state=status, investigation_reason=reason)
    elif kind == "node_state_changed":
        node = state["nodes"][payload["node_id"]]
        if payload["state"] not in ("done", "blocked"):
            raise ContractError("Only terminal exploration states may be assigned")
        if payload["state"] == "done" and any(state["nodes"][child]["exploration_state"] not in ("done", "blocked")
                                               for child in node["child_ids"]):
            raise ContractError("Node still has unexplored children")
        if payload["state"] == "done" and any(state["attempts"][key]["status"] == "pending" for key in node["attempts"]):
            raise ContractError("Node still has an unprocessed attempt")
        if node["exploration_state"] in ("done", "blocked") and node["exploration_state"] != payload["state"]:
            raise ContractError("Terminal node state cannot be overwritten")
        node["exploration_state"] = payload["state"]
        if state["current_id"] == node["id"]:
            state["current_id"] = node["parent_id"]
    elif kind == "node_attempt_started":
        node = state["nodes"][payload["node_id"]]
        attempt_id = validate_id(payload["attempt_id"], attempt=True)
        if (attempt_id != f"A{len(state['attempts']) + 1:04d}" or node["exploration_state"] in ("done", "blocked")
                or not isinstance(event.get("call_id"), str) or not event["call_id"] or len(event["call_id"]) > 128
                or any(a["call_id"] == event["call_id"] for a in state["attempts"].values())):
            raise ContractError("Invalid or reused attempt/call identity")
        state["attempts"][attempt_id] = {"attempt_id": attempt_id, "node_id": node["id"],
            "call_id": event["call_id"], "exec_id": None, "status": "pending", "started_seq": event["seq"],
            "result_path": f"work/node-results/{attempt_id}.json"}
        node["attempts"].append(attempt_id)
    elif kind == "node_attempt_failed":
        attempt = state["attempts"][payload["attempt_id"]]
        if attempt["status"] != "pending" or payload["reason"] not in (
                "missing_result", "invalid_result", "execution_failed", "interrupted", "not_executed"):
            raise ContractError("Invalid attempt failure")
        exec_id = payload["exec_id"]
        if exec_id is not None:
            _execution(state, attempt, exec_id, interrupted=True)
        elif any(item["call_id"] == attempt["call_id"] for item in state["executions"].values()):
            raise ContractError("Attempt has execution provenance")
        ref = _artifact_ref(payload["result_ref"]) if payload["result_ref"] else None
        attempt.update(status="failed", exec_id=exec_id, reason=payload["reason"], result_ref=ref)
        node = state["nodes"][attempt["node_id"]]
        _invalidate_failed_seed_attempt(state, node, event["seq"])
        node["verification_state"] = "inconclusive"
        node["verification"] = {"verdict": "inconclusive", "reason": payload["reason"], "source": "attempt_failure"}
        if ref:
            node["evidence_refs"].append(ref)
    elif kind == "node_verification_recorded":
        # Keep references from old journals without applying the removed lab verdict.
        attempt = state["attempts"][payload["attempt_id"]]
        if attempt["status"] != "imported":
            raise ContractError("Legacy lab event requires an imported attempt")
        node = state["nodes"][attempt["node_id"]]
        refs = [_artifact_ref(ref) for ref in payload["evidence_refs"]]
        proof_ref = _artifact_ref(payload["proof_ref"])
        node["evidence_refs"].extend([proof_ref, *refs])
        node["verification_state"] = "inconclusive"
        node["verification"]["reason"] = "legacy_lab_verdict_unavailable"
        node["verification"]["legacy_lab_proof_ref"] = proof_ref
    elif kind == "node_result_imported":
        attempt = state["attempts"][payload["attempt_id"]]
        digest = _digest({"payload": payload, "call_id": event["call_id"], "exec_id": event["exec_id"]})
        if attempt["status"] == "imported":
            if attempt["import_digest"] != digest:
                raise ContractError("Conflicting repeated result import")
            state["last_event_seq"] = event["seq"]
            return
        if attempt["status"] != "pending":
            raise ContractError("A closed failed attempt cannot import later files")
        if event["call_id"] != attempt["call_id"]:
            raise ContractError("Imported result call identity mismatch")
        status = _execution(state, attempt, event["exec_id"], interrupted=payload["execution_status"] == "interrupted")
        if payload["execution_status"] != status:
            raise ContractError("Imported result execution status mismatch")
        result_ref = _artifact_ref(payload["result_ref"])
        refs = [_artifact_ref(ref) for ref in payload["evidence_refs"]]
        runtime_refs = [{"path": ref["runtime_path"], "description": ref.get("description", "")}
                        for ref in refs if "runtime_path" in ref]
        result = validate_result({**payload["result"], "evidence_refs": runtime_refs},
                                 attempt_id=attempt["attempt_id"], node_id=attempt["node_id"])
        attempt.update(status="imported", execution_status=status, exec_id=event["exec_id"],
                       result_ref=result_ref, evidence_refs=refs, result=result,
                       imported_seq=event["seq"], import_digest=digest)
        node = state["nodes"][attempt["node_id"]]
        if "observation_policy" in payload:
            if payload["observation_policy"] not in POLICIES:
                raise ContractError("Unknown observation verification policy")
            records = {key: item for key, item in state.get("http_exchanges", {}).items()
                       if item["attempt_id"] == attempt["attempt_id"] and item["exec_id"] == event["exec_id"]}
            if status != "succeeded":
                records = {}
            verified = verify_observations(result, node["locator"], records,
                                           policy=payload["observation_policy"])
            node["observation_verification"] = verified
            attempt["observation_verification"] = copy.deepcopy(verified)
            _invalidate_children(state, node["parent_id"])
            _invalidate_children(state, node["id"])
        if "observations" in result and (status == "succeeded" or not node.get("seeded")):
            if node["observations"] != result["observations"]:
                node["observations"] = result["observations"]
                _invalidate_children(state, node["parent_id"])
                _invalidate_children(state, node["id"])
        node["evidence_refs"].extend([result_ref, *refs])
        node["verification_state"] = "inconclusive"
        node["verification"] = {"proposed_verdict": result["proposed_verdict"], "claim": result["claim"],
                                "verdict": "inconclusive", "reason": "no_automatic_verdict",
                                "source": "model_claim", "attempt_id": attempt["attempt_id"], "policy_version": None,
                                "next_observation": result["next_observation"]}
        lead_markers = payload.get("unverified_leads", [])
        if "unverified_lead_policy" in payload or "unverified_leads" in payload:
            if (payload.get("unverified_lead_policy") != UNVERIFIED_LEAD_POLICY
                    or payload.get("observation_policy") not in POLICIES or status != "succeeded"
                    or not isinstance(lead_markers, list) or not lead_markers
                    or len(lead_markers) > UNVERIFIED_LEAD_LIMIT
                    or sum(item.get("origin") == "unverified_lead" for item in state["nodes"].values())
                    + len(lead_markers) > UNVERIFIED_LEAD_LIMIT):
                raise ContractError("Invalid unverified lead policy")
        markers_by_key = {}
        for marker in lead_markers:
            if (not isinstance(marker, dict) or set(marker) != {"candidate_key", "signal", "field", "capture_request_ids"}
                    or not isinstance(marker["candidate_key"], str)
                    or not re.fullmatch(r"[0-9a-f]{64}", marker["candidate_key"])
                    or marker["candidate_key"] in markers_by_key
                    or marker["field"] not in ("security_signal", "query_behavior")
                    or marker["signal"] != result["observations"].get(marker["field"])
                    or (marker["field"] == "query_behavior" and marker["signal"] != "controlled_difference")
                    or marker["signal"] not in UNVERIFIED_LEAD_HYPOTHESIS_TYPES
                    or (marker["field"] == "security_signal" and marker["signal"] == "controlled_difference")
                    or node["kind"] != "surface" or _under_unverified_lead(state, node)
                    or node["observation_verification"]["fields"].get(marker["field"], {}).get("state")
                    not in ("unsupported", "contradicted")
                    or marker["capture_request_ids"] != sorted(records)):
                raise ContractError("Invalid unverified lead provenance")
            markers_by_key[marker["candidate_key"]] = marker
        if status == "succeeded":
            for discovery in result["discovered"]:
                marker = (markers_by_key.get(dedup_key(discovery))
                          if markers_by_key and discovery["kind"] == "candidate" else None)
                if marker and ("observations" in discovery or discovery.get("evidence_refs")
                               or discovery["hypothesis"]["type"] != UNVERIFIED_LEAD_HYPOTHESIS_TYPES[marker["signal"]]
                               or dedup_key(discovery) in {item["dedup_key"] for item in state["nodes"].values()}):
                    raise ContractError("Unverified lead must be a new unknown candidate")
                child_id = _discover(state, node["id"], discovery)
                child = state["nodes"][child_id]
                if marker:
                    child.update(origin="unverified_lead", source_attempt_id=attempt["attempt_id"],
                                 capture_request_ids=list(marker["capture_request_ids"]),
                                 unverified_signal=marker["signal"], unverified_field=marker["field"])
                    del markers_by_key[marker["candidate_key"]]
                if payload.get("observation_policy") in POLICIES and "observations" in discovery:
                    child["observation_verification"] = verify_observations(
                        discovery, child["locator"], records, policy=payload["observation_policy"])
                    _invalidate_children(state, child["parent_id"])
                    _invalidate_children(state, child_id)
                paths = {ref["path"] for ref in discovery.get("evidence_refs", [])}
                # Only explicitly attributed, captured evidence belongs to the
                # child. Parent evidence must not leak into unrelated siblings.
                for ref in refs:
                    if ref.get("runtime_path") in paths and ref not in child["evidence_refs"]:
                        child["evidence_refs"].append(copy.deepcopy(ref))
            if markers_by_key:
                raise ContractError("Unverified lead discovery is missing")
            review_ref = payload.get("seed_revalidation_evidence")
            if payload.get("observation_policy") in POLICIES and node.get("seeded"):
                from engine.observation_verification import _matches
                matching = [record for record in records.values()
                            if _matches(record["facts"], node["locator"])
                            and record["facts"]["url"] == node["locator"]["url"]]
                if matching and len({record["facts"]["status"] for record in matching}) == 1:
                    record = matching[-1]
                    if (node.get("seed_revalidation_pending") and not result["next_observation"].strip()
                            and attempt["started_seq"] > node.get("seed_dependency_invalidated_seq", 0)):
                        node["seed_revalidation_pending"] = False
                        state["seed_dependency_revision"] = event["seq"]
                    if (node["locator"]["method"] == "GET" and node["locator"]["auth_context"] == "anonymous"
                            and type(node.get("seed_prior_observations", {}).get("response_status")) is int):
                        attempt["evidence_refs"].append(record["response"])
                        _apply_seed_freshness(state, {"node_id": node["id"], "attempt_id": attempt["attempt_id"],
                            "response_status": record["facts"]["status"], "evidence_ref": record["response"]}, event["seq"])
            if review_ref is not None and (not isinstance(review_ref, dict) or review_ref not in refs
                                           or review_ref.get("source") != "model_file"):
                raise ContractError("Invalid seed revalidation evidence")
            if (node.get("seed_revalidation_pending") and review_ref and not result["next_observation"].strip()
                    and attempt["started_seq"] > node.get("seed_dependency_invalidated_seq", 0)):
                node["seed_revalidation_pending"] = False
                state["seed_dependency_revision"] = event["seq"]
            if "seed_freshness" in payload:
                _apply_seed_freshness(state, {"node_id": node["id"], "attempt_id": attempt["attempt_id"],
                                             **payload["seed_freshness"]}, event["seq"])
        elif node.get("seeded"):
            _invalidate_failed_seed_attempt(state, node, event["seq"])
    state["last_event_seq"] = event["seq"]


def replay_tree(events: list[dict], run_id: str) -> dict:
    """Pure reducer. No filesystem writes, process restoration or network calls."""
    state = _empty_state(run_id)
    for event in events:
        try:
            _apply(state, event)
        except (ValueError, KeyError, TypeError) as exc:
            raise LogFailure(f"Invalid tree event at sequence {state['last_event_seq'] + 1}: {exc}") from exc
    return state


def _safe_path(base: Path, relative: str) -> Path:
    path = base / safe_relative_path(relative)
    if not path.resolve().is_relative_to(base.resolve()):
        raise ContractError("File escapes its storage root")
    current = base
    for component in (None, *path.relative_to(base).parts):
        if component is not None:
            current = current / component
        if current.exists() or current.is_symlink():
            info = current.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ContractError("Linked storage paths are not allowed")
    return path


def _read_bounded(base: Path, relative: str, limit: int) -> bytes:
    path = _safe_path(base, relative)
    if not path.is_file() or path.stat().st_size > limit:
        raise ContractError("Evidence is missing, not a regular file, or too large")
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ContractError("Evidence exceeds size limit")
    return data


def _json_result(raw: bytes) -> dict:
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ContractError("Duplicate result field")
            value[key] = item
        return value

    def reject_constant(_value):
        raise ContractError("Non-finite JSON number")

    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=unique, parse_constant=reject_constant)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ContractError("Invalid result JSON") from exc


class TreeStore:
    def __init__(self, log: SessionLog):
        if log.metadata.get("experiment_mode") != "v4":
            raise ContractError("Tree storage requires an explicit v4 session")
        self.log = log
        self._state = _empty_state(log.run_id)
        self._snapshot_failed = False

    @property
    def state(self) -> dict:
        return copy.deepcopy(self._state)

    def _sync(self) -> None:
        if self._snapshot_failed:
            raise LogFailure("Tree snapshot failed; rebuild before further mutation")
        self._state = replay_tree(self.log.snapshot_events(), self.log.run_id)

    def _snapshot(self) -> None:
        try:
            directory = _safe_path(self.log.run_dir, "nodes")
            directory.mkdir(exist_ok=True)
            seq = self._state["last_event_seq"]
            for node_id, node in self._state["nodes"].items():
                path = _safe_path(self.log.run_dir, f"nodes/{validate_id(node_id)}.json")
                # The index is replaced last; mixed-generation node snapshots
                # are detected by last_event_seq and rebuilt from events.
                atomic_write_json(path, {**node, "last_event_seq": seq,
                                        "attempts": [self._state["attempts"][key] for key in node["attempts"]]})
            index = {key: self._state[key] for key in ("schema_version", "run_id", "root_id", "current_id", "last_event_seq")}
            index["node_ids"] = list(self._state["nodes"])
            atomic_write_json(_safe_path(self.log.run_dir, "tree.json"), index)
            if self._state["scorer"]["configuration"] is not None or self._state["scorer"]["halt"] is not None:
                scorer = self._state["scorer"]
                totals = scorer_usage(scorer)
                atomic_write_json(_safe_path(self.log.run_dir, "scorer.json"),
                    {"schema_version": 1, "last_event_seq": seq, "configuration": scorer["configuration"],
                     "usage": totals, "halt": scorer["halt"], "request_ids": list(scorer["requests"])})
                fields = {"scorer_usage": totals, "scorer_halt": scorer["halt"]}
                if scorer["configuration"]:
                    config = scorer["configuration"]
                    fields.update(scorer_effective_settings=config["settings"], scorer_model_pin=config["model_pin"],
                                  scorer_contract_version=config["contract_version"], scorer_validation="catalog_verified")
                self.log.update(**fields)
        except (OSError, ValueError) as exc:
            self._snapshot_failed = True
            raise LogFailure("Tree snapshot write failed; committed events remain authoritative") from exc

    def _commit(self, kind: str, payload: dict, **identity) -> None:
        prospective = {"run_id": self.log.run_id, "seq": self.log.latest_seq + 1, "kind": kind,
                       "tree_schema": SCHEMA_VERSION, "payload": payload, **identity}
        candidate = copy.deepcopy(self._state)
        try:
            _apply(candidate, prospective)
        except (KeyError, TypeError) as exc:
            raise ContractError("Invalid tree transition") from exc
        self.log.append(kind, {"tree_schema": SCHEMA_VERSION, "payload": payload, **identity})
        self._state = candidate
        self._snapshot()

    def initialize(self, target: str) -> str:
        with self.log.locked_writer():
            self._sync()
            self._commit("tree_initialized", {"target": normalize_url(target)})
            return self._state["root_id"]

    def refresh(self) -> dict:
        with self.log.locked_writer():
            self._sync()
            return self.state

    def record_scoring(self, kind: str, payload: dict) -> None:
        if kind not in SCORING_EVENTS:
            raise ContractError("Unsupported scorer event")
        with self.log.locked_writer():
            self._sync()
            self._commit(kind, payload)

    def set_observations(self, node_id: str, value: dict) -> None:
        """Replace categorical facts. Free text is dropped; this never confirms."""
        with self.log.locked_writer():
            self._sync()
            node_id = validate_id(node_id)
            value = observations(value)
            if self._state["nodes"][node_id]["observations"] != value:
                self._commit("node_observations_changed", {"node_id": node_id, "observations": value})

    def discover(self, parent_id: str, discovery: dict) -> str:
        with self.log.locked_writer():
            self._sync()
            discovery = validate_discovery(discovery)
            _omit_redundant_route_template(discovery)
            self._commit("node_discovered", {"parent_id": validate_id(parent_id), "discovery": discovery})
            return next(node["id"] for node in self._state["nodes"].values() if node["dedup_key"] == dedup_key(discovery))

    def seed_discover(self, parent_id: str, discovery: dict) -> str:
        """Replay a validated source discovery without changing its historical identity."""
        with self.log.locked_writer():
            self._sync()
            discovery = validate_discovery(discovery)
            self._commit("node_discovered", {"parent_id": validate_id(parent_id), "discovery": discovery})
            return next(node["id"] for node in self._state["nodes"].values() if node["dedup_key"] == dedup_key(discovery))

    def mark_seeded(self, node_id: str, source_run_id: str, source_node_id: str,
                    prior: dict, reused: bool = False) -> None:
        with self.log.locked_writer():
            self._sync()
            self._commit("node_seeded", {"node_id": validate_id(node_id), "source_run_id": validate_run_id(source_run_id),
                          "source_node_id": validate_id(source_node_id), "observations": observations(prior), "reused": reused})

    def request_seed_reinvestigation(self, node_id: str) -> None:
        with self.log.locked_writer():
            self._sync()
            self._commit("node_seed_reinvestigation_requested", {"node_id": validate_id(node_id)})

    def record_seed_dependency_verification(self, child_id: str, parent_id: str,
                                            attempt_id: str, evidence_ref: dict) -> None:
        """Record an operator's review, not an automatic model verdict."""
        with self.log.locked_writer():
            self._sync()
            self._commit("node_seed_dependency_verified", {"child_id": validate_id(child_id),
                         "parent_id": validate_id(parent_id), "attempt_id": validate_id(attempt_id, attempt=True),
                         "evidence_ref": copy.deepcopy(evidence_ref)})

    def select_node(self, node_id: str) -> None:
        with self.log.locked_writer():
            self._sync()
            self._commit("node_selected", {"node_id": validate_id(node_id)})

    def finish_investigation(self, node_id: str, *, status="done", reason=None) -> None:
        with self.log.locked_writer():
            self._sync()
            self._commit("node_investigation_finished", {"node_id": validate_id(node_id),
                                                        "status": status, "reason": reason})

    def set_node_state(self, node_id: str, state: str) -> None:
        with self.log.locked_writer():
            self._sync()
            self._commit("node_state_changed", {"node_id": validate_id(node_id), "state": state})

    def begin_attempt(self, node_id: str, *, call_id: str) -> dict:
        with self.log.locked_writer():
            self._sync()
            attempt_id = f"A{len(self._state['attempts']) + 1:04d}"
            result_path = _safe_path(self.log.run_dir / "runtime", f"work/node-results/{attempt_id}.json")
            if result_path.exists():
                raise ContractError("Expected result path already exists before this attempt")
            self._commit("node_attempt_started", {"attempt_id": attempt_id, "node_id": validate_id(node_id)}, call_id=call_id)
            return copy.deepcopy(self._state["attempts"][attempt_id])

    def import_result(self, attempt_id: str, *, exec_id: str, interrupted: bool = False) -> dict:
        """Import after execution has finished. For interrupted recovery the caller
        must first stop runtime writers; this method never stops or runs anything.
        """
        with self.log.locked_writer():
            self._sync()
            attempt = self._state["attempts"].get(validate_id(attempt_id, attempt=True))
            if attempt is None:
                raise ContractError("Unknown attempt")
            if attempt["status"] == "imported":
                if attempt["exec_id"] != exec_id:
                    raise ContractError("Repeated result uses a different execution")
                return copy.deepcopy(attempt)
            status = _execution(self._state, attempt, exec_id, interrupted=interrupted)
            runtime = self.log.run_dir / "runtime"
            raw = _read_bounded(runtime, attempt["result_path"], MAX_RESULT_BYTES)
            result = validate_result(_json_result(raw), attempt_id=attempt_id, node_id=attempt["node_id"])
            # Persist a stable input location in new events. The original
            # model result is preserved below; replay of older events keeps
            # its original reducer semantics.
            for discovery in result["discovered"]:
                locator = discovery["locator"]
                parts = urlsplit(locator["url"])
                if parts.query:
                    names = sorted({name for name, _ in parse_qsl(parts.query, keep_blank_values=True)})
                    locator["url"] = urlunsplit((parts.scheme, parts.netloc, parts.path,
                                                  urlencode([(name, "") for name in names]), ""))
                _omit_redundant_route_template(discovery)
            captured = []
            total = 0
            for ref in result["evidence_refs"]:
                data = _read_bounded(runtime, ref["path"], MAX_EVIDENCE_BYTES)
                total += len(data)
                if total > MAX_TOTAL_EVIDENCE_BYTES:
                    raise ContractError("Combined evidence exceeds size limit")
                captured.append((ref, data))
            # Enrich only an explicitly matching surface's candidate. Root facts
            # and unrelated locations must never be copied to new children.
            node = self._state["nodes"][attempt["node_id"]]
            policy = (self._state["scorer"].get("configuration") or {}).get("projection_version")
            policy = policy or (self.log.metadata.get("scorer_settings") or {}).get("reevaluation_policy")
            records = []
            verified = None
            if policy in POLICIES:
                from engine.http_capture import check_capture_records
                records = [record for record in self._state.get("http_exchanges", {}).values()
                           if record["attempt_id"] == attempt_id and record["exec_id"] == exec_id]
                check_capture_records(self.log.run_dir, records)
                verified = verify_observations(result, node["locator"],
                                               {record["request_id"]: record for record in records}, policy=policy)
            if status == "succeeded" and node["kind"] == "surface":
                for discovery in result["discovered"]:
                    if (discovery["kind"] == "candidate"
                            and dedup_key({**discovery, "kind": "surface", "hypothesis": None}) == node["dedup_key"]):
                        if "observations" in result:
                            discovery.setdefault("observations", copy.deepcopy(result["observations"]))
                        if "observation_evidence" in result:
                            discovery.setdefault("observation_evidence", copy.deepcopy(result["observation_evidence"]))
                        discovery.setdefault("evidence_refs", copy.deepcopy(result["evidence_refs"]))
            followup_input = result
            if verified is not None:
                supported_facts = {field: value for field, value in result.get("observations", {}).items()
                                   if verified["fields"].get(field, {}).get("state") == "supported"
                                   and verified["fields"][field].get("value") == value}
                followup_input = {**result, "observations": supported_facts}
            followups = followup_candidates(node, followup_input) if status == "succeeded" else []
            followups = followups[:max(0, 100 - len(result["discovered"]))]
            result["discovered"].extend(followups)
            lead_suggestions = []
            if (status == "succeeded" and policy in POLICIES and not _under_unverified_lead(self._state, node)):
                remaining = min(MAX_UNVERIFIED_LEADS_PER_RUN - sum(
                    item.get("origin") == "unverified_lead" for item in self._state["nodes"].values()),
                    100 - len(result["discovered"]))
                if remaining > 0:
                    lead_suggestions = unverified_lead_candidates(
                        node, result, verified,
                        existing_keys={item["dedup_key"] for item in self._state["nodes"].values()})[:remaining]
                    result["discovered"].extend(item["discovery"] for item in lead_suggestions)
            for discovery in result["discovered"]:
                # Absent attribution is a legacy journal convention only. New
                # discoveries without a binding cannot borrow all parent files.
                discovery.setdefault("evidence_refs", [])
            result = validate_result(result, attempt_id=attempt_id, node_id=attempt["node_id"])
            result_ref = {**self.log.preserve_bytes(f"node-results/{attempt_id}.json", raw), "source": "model_result"}
            refs = []
            for index, (ref, data) in enumerate(captured):
                suffix = Path(ref["path"]).suffix
                saved = self.log.preserve_bytes(f"node-evidence/{attempt_id}/{index:04d}{suffix}", data)
                refs.append({**saved, "description": ref["description"], "source": "model_file", "runtime_path": ref["path"]})
            result.pop("evidence_refs")
            payload = {"attempt_id": attempt_id, "execution_status": status,
                       "result": result, "result_ref": result_ref, "evidence_refs": refs}
            if policy in POLICIES:
                payload["observation_policy"] = policy
            if lead_suggestions:
                payload["unverified_lead_policy"] = UNVERIFIED_LEAD_POLICY
                payload["unverified_leads"] = [{"candidate_key": dedup_key(item["discovery"]),
                                                "signal": item["signal"], "field": item["field"],
                                                "capture_request_ids": sorted(record["request_id"] for record in records)}
                                               for item in lead_suggestions]
            if followups:
                payload["followup_policy"] = "observed-security-signal-v1"
                payload["generated_candidate_count"] = len(followups)
            node = self._state["nodes"][attempt["node_id"]]
            prior = node.get("seed_prior_observations", {}).get("response_status")
            if policy not in POLICIES and status == "succeeded" and node.get("seed_revalidation_pending") and not result["next_observation"].strip():
                locator = node["locator"]
                matched = [(ref, _response_status_evidence(data, locator["url"], locator["method"], locator["auth_context"]))
                           for ref, (_, data) in zip(refs, captured)]
                matched = [(ref, value) for ref, value in matched if value is not None]
                if matched and len({value for _, value in matched}) == 1:
                    payload["seed_revalidation_evidence"] = matched[0][0]
            if (policy not in POLICIES and status == "succeeded" and node.get("seeded") and type(prior) is int
                    and node["locator"]["method"] == "GET" and node["locator"]["auth_context"] == "anonymous"):
                matched = [(ref, _response_status_evidence(data, node["locator"]["url"]))
                           for ref, (_, data) in zip(refs, captured)]
                matched = [(ref, value) for ref, value in matched if value is not None]
                if matched and len({value for _, value in matched}) == 1:
                    ref, value = matched[0]
                    payload["seed_freshness"] = {"response_status": value, "evidence_ref": ref}
            self._commit("node_result_imported", payload, call_id=attempt["call_id"], exec_id=exec_id)
            return copy.deepcopy(self._state["attempts"][attempt_id])

    def fail_attempt(self, attempt_id: str, *, reason: str, exec_id: str | None = None) -> None:
        with self.log.locked_writer():
            self._sync()
            attempt = self._state["attempts"][validate_id(attempt_id, attempt=True)]
            if attempt["status"] == "failed":
                return
            ref = None
            try:
                raw = _read_bounded(self.log.run_dir / "runtime", attempt["result_path"], MAX_EVIDENCE_BYTES)
            except FileNotFoundError:
                raw = None
            except (OSError, ValueError) as exc:
                # Missing files are expected; unreadable/linked/oversized files
                # must remain available for recovery instead of being deleted.
                path = self.log.run_dir / "runtime" / attempt["result_path"]
                if path.exists() or path.is_symlink():
                    raise LogFailure("Cannot preserve failed attempt result") from exc
                raw = None
            if raw is not None:
                ref = self.log.preserve_bytes(f"invalid-node-results/{attempt_id}.json", raw)
            self._commit("node_attempt_failed", {"attempt_id": attempt_id, "exec_id": exec_id,
                                                 "reason": reason, "result_ref": ref})

    def rebuild(self) -> dict:
        with self.log.locked_writer():
            state = replay_tree(self.log.snapshot_events(), self.log.run_id)
            if state.get("http_exchanges"):
                from engine.http_capture import check_capture_records
                try:
                    check_capture_records(self.log.run_dir, state["http_exchanges"].values())
                except (OSError, ValueError, KeyError, TypeError) as exc:
                    raise LogFailure("Cannot verify captured tree evidence") from exc
            if state["root_id"] is None:
                raise LogFailure("Tree initialization event is missing")
            for node in state["nodes"].values():
                for ref in node["evidence_refs"]:
                    try:
                        data = _read_bounded(self.log.run_dir, ref["path"], MAX_EVIDENCE_BYTES)
                        if len(data) != ref["bytes"] or hashlib.sha256(data).hexdigest() != ref["sha256"]:
                            raise ContractError("Preserved evidence changed")
                    except (OSError, ValueError) as exc:
                        raise LogFailure("Cannot verify preserved tree evidence") from exc
            self._state = state
            self._snapshot()
            self._snapshot_failed = False
            return self.state

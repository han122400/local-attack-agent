"""Greedy depth-first orchestration, separate from v3 and deterministic replay."""

import json
import threading
import time
from dataclasses import replace
from urllib.parse import urlsplit

from code_checks import check_generated_code
from harness import Harness, ModelFailure
from model_providers import is_openrouter
from planning import load_prompt
from session_log import atomic_write_json
from scorer_session import RiskScorer
from v4_usage import summarize_usage
from v4_contracts import ContractError, MAX_RESULT_BYTES, RESULT_SCHEMA, validate_result
from v4_policy import V4BudgetStop, execution_settings
from v4_tree import (TreeStore, MAX_EVIDENCE_BYTES, _json_result, _read_bounded, _safe_path,
                     latest_observation, investigation_gaps)
from risk_rubric import observations, OBSERVATIONS_SCHEMA


class BudgetGate:
    def __init__(self, log, *, stop_event, clock=time.monotonic):
        self.log, self.stop_event, self.clock = log, stop_event, clock
        self.settings = execution_settings(log.metadata.get("v4_settings"))
        self.budgets = log.metadata["budgets"]
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
            system_prompt=prompt, system_prompt_version="v4", system_prompt_sha256=digest, before_request=self.gate.before_model)
        self.scorer = RiskScorer(self.tree, adapter=scorer_adapter, admit_request=self.gate.admit)
        self.history = []
        from explorer import context_char_budget
        self.context_budget = context_char_budget(log.metadata["model_settings"]["options"].get("num_ctx", 8192), prompt)

    def _context(self, node_id, *, unproductive=0, node_tools=0):
        state = self.tree.refresh()
        node = state["nodes"][node_id]
        def public(item):
            return {key: item[key] for key in ("id", "kind", "locator", "hypothesis", "observations")}
        path, current = [], node
        while current["parent_id"]:
            current = state["nodes"][current["parent_id"]]
            path.insert(0, public(current))
        attempt_id = f"A{len(state['attempts']) + 1:04d}"
        siblings = state["nodes"][node["parent_id"]]["child_ids"] if node["parent_id"] else []
        ranks = [{"node_id": key, "score": state["nodes"][key]["risk"].get("score")} for key in siblings]
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
                                    "evidence_refs": [], "next_observation": ""},
                "result_schema": RESULT_SCHEMA, "previous_result": previous,
                "recent": self.history[-2:], "sibling_ranks": ranks[:20], "total_siblings": len(siblings),
                "remaining": {"seconds": max(0, int(self.gate.deadline - self.gate.clock())),
                              "model_calls": self.gate.budgets["model_calls"] - self.gate.usage()["model_calls"],
                              "node_attempts": max(0, self.settings["node_attempts"] - unproductive),
                              "node_tool_calls": max(0, self.settings["node_tool_calls"] - node_tools)}}
        while len(json.dumps(value, ensure_ascii=False)) > self.context_budget and (value["recent"] or value["parent_path"]):
            (value["recent"] if value["recent"] else value["parent_path"]).pop(0)
        while len(json.dumps(value, ensure_ascii=False)) > self.context_budget and previous and previous["evidence_refs"]:
            previous["evidence_refs"].pop()
            previous["details_truncated"] = True
        if len(json.dumps(value, ensure_ascii=False)) > self.context_budget and previous:
            previous["claim"], previous["details_truncated"] = "", True
        if len(json.dumps(value, ensure_ascii=False)) > self.context_budget:
            raise V4BudgetStop("context_budget")
        return value

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
                        pending_followup=(context["previous_result"] or {}).get("next_observation", "")))
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
                                 "stdout": output.stdout[-2500:], "stderr": output.stderr[-1000:]})
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
                repair["instruction"] = ("The preceding tool already ran for this attempt but its result JSON is "
                    + reason + ". Use action=write_file, path=attempt.result_path, and content as a JSON string "
                    "made from result_template. Include specific discovered input surfaces if observed. "
                    "Keep every template field. evidence_refs may refer only to separately written observation files, "
                    "never a node-results JSON or an absolute path. Do not run another target request in this response.")
                def validate_correction(candidate):
                    if candidate.action != "write_file" or candidate.path != attempt["result_path"]:
                        raise ValueError("Use write_file at the exact pending attempt.result_path")
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
    def _validate_command(command, result_path, *, pending_followup=""):
        if command.action == "finish" and pending_followup.strip():
            raise ValueError("A follow-up observation is pending. Investigate previous_result.next_observation and write a new result before finish")
        if command.action == "run_python":
            problems = check_generated_code(command.code)
            if problems:
                raise ValueError("; ".join(problems[:3]))
        if command.action == "run_python" and result_path not in command.code:
            raise ValueError("run_python must write result JSON to attempt.result_path before it exits")
        return command

    def run(self):
        try:
            current = self.tree.state["root_id"]
            while current:
                self.gate.check()
                node = self.tree.refresh()["nodes"][current]
                if node["investigation_state"] == "pending":
                    self._investigate(current)
                node = self.tree.refresh()["nodes"][current]
                pending = [key for key in node["child_ids"] if self.tree.state["nodes"][key]["exploration_state"] not in ("done", "blocked")]
                if pending:
                    ranked = self.scorer.score_children(current)
                    if not ranked["ready"]:
                        if ranked["stop_reason"] == "context_changed":
                            continue
                        raise V4BudgetStop(ranked["stop_reason"])
                    selected = ranked["ordered_node_ids"][0]
                    self.log.append("v4_node_selection", {"parent_id": current, "node_id": selected,
                        "score": ranked["scores"].get(selected), "degraded_mode": selected in ranked["unscored"]})
                    current = selected
                else:
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

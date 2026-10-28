"""Greedy depth-first orchestration, separate from v3 and deterministic replay."""

import json
import threading
import time
from urllib.parse import urlsplit

from harness import Harness, ModelFailure
from model_providers import is_openrouter
from planning import load_prompt
from scorer_session import RiskScorer
from v4_usage import summarize_usage
from v4_contracts import ContractError, RESULT_SCHEMA
from v4_oracle import LabOracle
from v4_policy import V4BudgetStop, execution_settings, supported_candidate
from v4_tree import TreeStore


class BudgetGate:
    def __init__(self, log, *, stop_event, clock=time.monotonic):
        self.log, self.stop_event, self.clock = log, stop_event, clock
        self.settings = execution_settings(log.metadata.get("v4_settings"))
        self.budgets = log.metadata["budgets"]
        self.deadline = clock() + self.budgets["seconds"]
        self.deep_reserve = min(self.settings["deep_reserve_calls"], self.budgets["model_calls"] // 4)
        log.update(v4_budget_policy={"version": "v4-budget-v1", "deep_reserve_calls": self.deep_reserve,
                                    "verification_reserve_seconds": self.settings["verification_reserve_seconds"]})

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
        if context.get("node", {}).get("id") == "N0001" and self.usage()["model_calls"] >= self.budgets["model_calls"] - self.deep_reserve:
            raise V4BudgetStop("node_limit")
        self.log.append("v4_execution_reserved", {"call_id": call_id, "remote": remote, "estimate_usd": estimate})


class V4Runner:
    def __init__(self, log, runtime, *, transport=None, scorer_adapter=None, oracle=None, stop_event=None, clock=time.monotonic):
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
        self.oracle = oracle or LabOracle(log, self.settings, admit=lambda timeout: self.gate.admit("oracle", 0, timeout))
        self.history = []
        from explorer import context_char_budget
        self.context_budget = context_char_budget(log.metadata["model_settings"]["options"].get("num_ctx", 8192), prompt)

    def _context(self, node_id):
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
        value = {"goal": self.log.metadata["goal"], "target": self.log.metadata["target"], "node": public(node),
                "parent_path": path[-8:], "runtime_path": str(self.runtime.runtime_dir), "python": self.runtime.python,
                "attempt": {"attempt_id": attempt_id, "node_id": node_id, "result_path": f"work/node-results/{attempt_id}.json"},
                "result_schema": RESULT_SCHEMA, "recent": self.history[-2:], "sibling_ranks": ranks[:20], "total_siblings": len(siblings),
                "remaining": {"seconds": max(0, int(self.gate.deadline - self.gate.clock())),
                              "model_calls": self.gate.budgets["model_calls"] - self.gate.usage()["model_calls"]}}
        while len(json.dumps(value, ensure_ascii=False)) > self.context_budget and (value["recent"] or value["parent_path"]):
            (value["recent"] if value["recent"] else value["parent_path"]).pop(0)
        if len(json.dumps(value, ensure_ascii=False)) > self.context_budget:
            raise V4BudgetStop("context_budget")
        return value

    def _investigate(self, node_id):
        attempts = 0
        self.tree.select_node(node_id)
        for _ in range(self.settings["node_tool_calls"]):
            self.gate.check()
            if attempts >= self.settings["node_attempts"]:
                break
            context = self._context(node_id)
            reserve = self.settings["verification_reserve_seconds"] if self.settings["exclusive_lab"] and supported_candidate(self.tree.state["nodes"][node_id]) else 0
            try:
                command = self.harness.decide(self.log.run_id, context, model=self.log.metadata["model"],
                    settings=self.log.metadata["model_settings"], deadline=self.gate.deadline - reserve)
            except V4BudgetStop as exc:
                if exc.reason == "node_limit":
                    self.log.append("v4_node_limit", {"node_id": node_id, "reason": "deep_calls_reserved"})
                    break
                raise
            except ModelFailure as exc:
                if exc.kind in ("budget", "cancelled", "timed_out"):
                    raise V4BudgetStop({"budget": "call_budget", "cancelled": "user_stopped", "timed_out": "time_budget"}[exc.kind])
                if exc.kind == "transport":
                    raise V4BudgetStop("model_unavailable")
                self.history.append({"node_id": node_id, "error": exc.kind})
                break
            if command.action == "finish":
                break
            if self.gate.check()["tool_calls"] >= self.gate.budgets["tool_calls"]:
                raise V4BudgetStop("call_budget")
            executed = command.action in ("run_command", "run_python")
            attempt, token = None, None
            if executed:
                attempt = self.tree.begin_attempt(node_id, call_id=command.call_id)
                if attempt["attempt_id"] != context["attempt"]["attempt_id"]:
                    raise ContractError("Attempt identity changed while deciding")
                attempts += 1
                token = self.oracle.begin(attempt, self.tree.state["nodes"][node_id])
            timeout = min(self.gate.budgets["command_seconds"], self.gate.deadline - self.gate.clock() - reserve)
            if timeout <= 0:
                raise V4BudgetStop("time_budget")
            output = self.runtime.run(self.log.run_id, command.runtime_payload(), call_id=command.call_id, timeout=timeout)
            self.history.append({"node_id": node_id, "action": command.action, "exit_code": output.exit_code,
                                 "stdout": output.stdout[-2500:], "stderr": output.stderr[-1000:]})
            if not attempt:
                continue
            try:
                imported = self.tree.import_result(attempt["attempt_id"], exec_id=output.exec_id)
            except (ValueError, OSError) as exc:
                reason = "missing_result" if not (self.runtime.runtime_dir / attempt["result_path"]).exists() else "invalid_result"
                self.tree.fail_attempt(attempt["attempt_id"], reason=reason, exec_id=output.exec_id)
                self.history[-1]["result_error"] = reason
                continue
            if imported["execution_status"] != "succeeded":
                continue
            origin = urlsplit(self.log.metadata["target"])
            allowed = (origin.scheme, origin.hostname, origin.port or (443 if origin.scheme == "https" else 80))
            for discovered in self.tree.state["nodes"].values():
                location = urlsplit(discovered["locator"]["url"])
                actual = (location.scheme, location.hostname, location.port or (443 if location.scheme == "https" else 80))
                if actual != allowed and discovered["exploration_state"] not in ("done", "blocked"):
                    self.tree.set_node_state(discovered["id"], "blocked")
                    self.log.append("v4_scope_excluded", {"node_id": discovered["id"]})
            if token:
                bound = {"attempt_id": attempt["attempt_id"], "node_id": node_id, "exec_id": output.exec_id}
                proof, refs = self.oracle.finish(token, bound, self.tree.state["nodes"][node_id])
                if proof:
                    self.tree.record_verification(attempt["attempt_id"], proof, refs)
            # An imported result completes investigation; descendants are the
            # harness's next work, not another model-selected global target.
            break
        self.tree.finish_investigation(node_id)

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
                self.oracle.close()

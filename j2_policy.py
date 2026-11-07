"""Optional feature-based investigation memory; notes are model statements, not verdicts."""

import copy
import ast
import json
import re
from pathlib import PurePosixPath
from urllib.parse import urlsplit, urlunsplit
from j2_progress import pipeline_view, public_path


MEMORY_PATH = "work/j2-memory.json"
EVIDENCE_LEVELS = {"hypothesis", "normal_behavior", "controlled_comparison", "security_effect"}


def bounded_discovery(summary):
    """Retain declarations, never response bodies or executable instructions."""
    if not isinstance(summary, dict):
        return None
    source = summary.get("source_url")
    try:
        parts = urlsplit(source) if isinstance(source, str) else None
        if (parts is None or len(source) > 2048 or parts.scheme not in {"http", "https"}
                or not parts.hostname or parts.username is not None or parts.password is not None
                or parts.port == 0 or re.search(r"[\x00-\x20\x7f\\]", source)):
            return None
    except ValueError:
        return None
    result = {"source_url": source, "assets": [], "surfaces": [], "hints": [],
              "truncated": bool(summary.get("truncated"))}
    fields = {"url", "kind", "locator", "method", "inputs", "input_known", "path_hint",
              "reason", "origin_unknown", "probe_url", "origin_assumed", "declared_only", "step"}
    size = len(json.dumps(result, ensure_ascii=False))
    for key in ("assets", "surfaces", "hints"):
        items = summary.get(key, [])
        if not isinstance(items, list):
            result["truncated"] = True
            continue
        result["truncated"] |= len(items) > 20
        for item in items[:20]:
            if not isinstance(item, dict):
                result["truncated"] = True
                continue
            try:
                encoded = json.dumps({k: v for k, v in item.items() if k in fields}, ensure_ascii=False)
            except (TypeError, ValueError):
                result["truncated"] = True
                continue
            if size + len(encoded) > 12_000:
                result["truncated"] = True
                continue
            result[key].append(json.loads(encoded))
            size += len(encoded) + 2
    return result


class CandidateNotebook:
    """One execution's explicitly submitted notes, separate from target text and stdout."""

    def __init__(self):
        self.notes = []

    def note_candidate(self, name, *, basis="", next_observation="", state="open", evidence_paths=(),
                       evidence_level=None, observation_steps=None):
        fields = {"name": name, "basis": basis, "next_observation": next_observation}
        limits = {"name": 160, "basis": 800, "next_observation": 800}
        if (any(not isinstance(value, str) or len(value) > limits[key] for key, value in fields.items())
                or not isinstance(name, str) or not name.strip()
                or not isinstance(state, str) or state not in {"open", "blocked", "closed"}
                or not isinstance(evidence_paths, (list, tuple)) or len(evidence_paths) > 4):
            return {"recorded": False, "error": "Use a short name, text details, open/blocked/closed and up to four evidence paths"}
        if (evidence_level is not None and (not isinstance(evidence_level, str) or evidence_level not in EVIDENCE_LEVELS)
                or observation_steps is not None and (not isinstance(observation_steps, (list, tuple))
                    or len(observation_steps) > 8 or any(type(step) is not int or not 1 <= step <= 500 for step in observation_steps))):
            return {"recorded": False, "error": "Use hypothesis/normal_behavior/controlled_comparison/security_effect and up to eight prior step numbers; these remain model claims"}
        for path in evidence_paths:
            if (not isinstance(path, str) or not path or len(path) > 256
                    or any(ord(char) < 32 or ord(char) == 127 or char in "\\:" for char in path)
                    or PurePosixPath(path).is_absolute() or ".." in PurePosixPath(path).parts
                    or len(PurePosixPath(path).parts) < 2
                    or PurePosixPath(path).parts[0] not in {"work", "tools"}):
                return {"recorded": False, "error": "Evidence paths must be runtime-relative work/ or tools/ paths"}
        if len(self.notes) >= 16:
            return {"recorded": False, "error": "Per-execution note limit reached"}
        note = {**fields, "name": name.strip(), "state": state, "evidence_paths": list(evidence_paths)}
        if evidence_level is not None:
            note["evidence_level"] = evidence_level
        if observation_steps is not None:
            note["observation_steps"] = list(dict.fromkeys(observation_steps))
        self.notes.append(note)
        return {"recorded": True, "verification": "model_statement_only"}


class J2Policy:
    def __init__(self):
        self.candidates = {}
        self.advisories = []
        self.omitted_notes = 0
        self.executions = []
        self.rejected_actions = []
        self.unavailable_modules = set()
        self.discovery_summaries = {}
        self.omitted_discovery = 0

    def command_problems(self, command):
        if command.action != "run_python":
            return []
        from code_checks import check_generated_code
        problems = check_generated_code(command.code)
        if problems or not self.unavailable_modules:
            return problems
        # Only initial unconditional imports: guarded fallbacks and environment repairs remain allowed.
        for node in ast.parse(command.code).body:
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                continue
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                names = [node.module or ""]
            else:
                break
            for name in names:
                if any(name == missing or name.startswith(missing + ".") for missing in self.unavailable_modules):
                    problems.append(f"Module {name} already failed to import in this runtime. Use provided helpers or a guarded fallback; do not repeat the same unavailable import.")
        return problems

    def record_rejection(self, command, problems, source_path, artifact_path):
        self.rejected_actions.append({"call_id": command.call_id, "action": command.action,
            "plan": command.plan[:160], "outcome": "not_run", "problems": problems[:3],
            "source_path": source_path, "artifact_path": artifact_path})

    def record(self, execution, step, command=None):
        partial = execution.exit_code != 0 or execution.timed_out or execution.cancelled
        if execution.exception_type == "ModuleNotFoundError":
            self.unavailable_modules.update(re.findall(r"No module named ['\"]([A-Za-z_][A-Za-z0-9_.]*)['\"]", execution.exception))
        if not partial and execution.action in {"write_file", "run_command"}:
            self.unavailable_modules.clear()
        path = execution.path
        source = ("tools/" + path.replace("\\", "/").rsplit("/", 1)[-1]
                  if execution.action == "run_python" and path.endswith(".py") else "")
        outcome = ("not_run" if execution.cause_code == "static_check" else "cancelled" if execution.cancelled
                   else "timed_out" if execution.timed_out else "failed" if partial else "executed")
        responses = []
        for item in (execution.http_observations if isinstance(execution.http_observations, list) else [])[:16]:
            if (not isinstance(item, dict) or not isinstance(item.get("url"), str)
                    or type(item.get("status")) is not int or not 100 <= item["status"] <= 599
                    or not isinstance(item.get("method"), str)
                    or item["method"] not in {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}):
                continue
            try:
                parts = urlsplit(item["url"])
                if parts.scheme not in {"http", "https"} or not parts.hostname:
                    continue
                url = urlunsplit((parts.scheme, parts.netloc.rsplit("@", 1)[-1], parts.path, "", ""))
            except ValueError:
                continue
            response = {"url": url[:240], "method": item["method"], "status": item["status"]}
            if len(url) > 240 or item.get("url_truncated") is True:
                response["url_truncated"] = True
            responses.append(response)
        helper_errors = []
        for item in (execution.helper_errors if isinstance(execution.helper_errors, list) else [])[:16]:
            if (isinstance(item, dict) and item.get("helper") == "goto_and_render"
                    and isinstance(item.get("error_type"), str) and isinstance(item.get("url"), str)):
                try:
                    parts = urlsplit(item["url"])
                    if parts.scheme not in {"http", "https"} or not parts.hostname:
                        continue
                    url = urlunsplit((parts.scheme, parts.netloc.rsplit("@", 1)[-1], parts.path, "", ""))
                except ValueError:
                    continue
                helper_errors.append({"helper": item["helper"], "error_type": item["error_type"][:80], "url": url[:240]})
        helper_outcomes = []
        for item in (execution.helper_outcomes if isinstance(execution.helper_outcomes, list) else [])[:16]:
            if (isinstance(item, dict) and item.get("helper") == "goto_and_render"
                    and item.get("outcome") == "completed" and isinstance(item.get("url"), str)):
                url = public_path(item["url"])
                if url:
                    helper_outcomes.append({"helper": "goto_and_render", "outcome": "completed", "url": url[:240]})
        for value in (execution.discovery_summaries if isinstance(execution.discovery_summaries, list) else [])[-4:]:
            summary = bounded_discovery(value)
            if summary is None:
                continue
            source_url = summary["source_url"]
            previous = self.discovery_summaries.pop(source_url, {})
            for key in ("assets", "surfaces", "hints"):
                entries = {}
                for entry in [{**item, "step": step} for item in summary[key]] + previous.get(key, []):
                    identity = json.dumps({k: v for k, v in entry.items() if k != "step"}, sort_keys=True)
                    entries.setdefault(identity, entry)
                summary[key] = list(entries.values())
            summary["truncated"] |= previous.get("truncated", False)
            summary = bounded_discovery(summary)
            self.discovery_summaries[source_url] = {**summary, "step": step, "source_path": source,
                                                    "verification": "provided_text_declarations_only"}
            if len(self.discovery_summaries) > 32:
                self.discovery_summaries.pop(next(iter(self.discovery_summaries)))
                self.omitted_discovery += 1
        self.executions.append({"step": step, "exec_id": execution.exec_id, "action": execution.action,
            "outcome": outcome, "source_path": source, "artifact_path": execution.artifact_path,
            "file_path": command.path if command and command.action in {"read_file", "write_file"} else "",
            "cause_code": execution.cause_code, "exception_type": execution.exception_type,
            "failure_kind": ("cancelled" if execution.cancelled else "execution_timeout" if execution.timed_out
                else "dependency" if partial and execution.exception_type == "ModuleNotFoundError"
                else "runtime" if partial and execution.failure_source == "system"
                else "harness_scope" if execution.blocked_requests
                else "code" if partial and (execution.exception_type or not helper_errors) else None),
            "plan": command.plan[:160] if command else "", "partial": bool(partial and (execution.stdout.strip() or responses)),
            "output_present": bool(execution.stdout.strip()), "verification": "execution_only",
            "http_observations": responses,
            "helper_errors": helper_errors,
            "helper_outcomes": helper_outcomes,
            "request_refs": [item["id"] for item in (execution.request_templates or [])
                             if isinstance(item, dict) and item.get("step") == execution.exec_id
                             and isinstance(item.get("id"), str)]})
        notes = execution.investigation_notes
        if not isinstance(notes, list):
            notes = []
        for note in notes[:16]:
            notebook = CandidateNotebook()
            if not isinstance(note, dict) or set(note) - {"name", "basis", "next_observation", "state", "evidence_paths", "evidence_level", "observation_steps"}:
                self.advisories.append(f"Step {step}: invalid candidate note ignored")
                continue
            try:
                accepted = notebook.note_candidate(**note)
            except TypeError:
                accepted = {"recorded": False}
            if not accepted["recorded"]:
                self.advisories.append(f"Step {step}: invalid candidate note ignored")
                continue
            item = notebook.notes[0]
            name = item["name"]
            previous = self.candidates.get(name, {})
            if not previous and len(self.candidates) >= 64:
                self.omitted_notes += 1
                self.advisories.append(f"Step {step}: candidate memory limit reached; raw notes remain in execution logs")
                continue
            if partial:
                item["state"] = "blocked" if item["state"] == "blocked" else "open"
            if item["state"] != "closed":
                item["next_observation"] = item["next_observation"] or previous.get("next_observation", "")
            item["basis"] = item["basis"] or previous.get("basis", "")
            item["evidence_level"] = item.get("evidence_level", previous.get("evidence_level", "hypothesis"))
            item["observation_steps"] = item.get("observation_steps", previous.get("observation_steps", []))
            available = {entry["step"] for entry in self.executions if entry["step"] < step
                         and entry["outcome"] == "executed" and entry["output_present"] and not entry["helper_errors"]}
            item["available_observation_steps"] = [value for value in item["observation_steps"] if value in available]
            item["unavailable_observation_steps"] = [value for value in item["observation_steps"] if value not in available]
            item["evidence_paths"] = list(dict.fromkeys(previous.get("evidence_paths", []) + item["evidence_paths"]))[-4:]
            if item["state"] == "closed" and item["next_observation"].strip():
                item["state"] = "open"
                self.advisories.append(f"Step {step}: follow-up retained for {name}")
            path = execution.path
            if path and path.endswith(".py"):
                path = "tools/" + path.replace("\\", "/").rsplit("/", 1)[-1]
            else:
                path = previous.get("source_path", "")
            self.candidates[name] = {**item, "step": step, "source_path": path,
                "first_source_path": previous.get("first_source_path") or path,
                "artifact_path": execution.artifact_path, "partial": bool(partial),
                "steps": (previous.get("steps", []) + [step])[-8:], "verification": "model_statement_only"}
        self.advisories = self.advisories[-4:]

    def investigation_status(self):
        if self.omitted_notes:
            return "incomplete"
        if not self.candidates:
            return "untracked"
        return "incomplete" if any(item["state"] != "closed" for item in self.candidates.values()) else "recorded_work_closed"

    def snapshot(self):
        return copy.deepcopy({"candidates": list(self.candidates.values()), "advisories": self.advisories,
                              "pipeline": pipeline_view(list(self.discovery_summaries.values()), self.executions, self.rejected_actions),
                              "executions": self.executions, "rejected_actions": self.rejected_actions,
                              "unavailable_modules": sorted(self.unavailable_modules),
                              "discovery_summaries": list(self.discovery_summaries.values()),
                              "omitted_discovery": self.omitted_discovery,
                              "omitted_notes": self.omitted_notes,
                              "investigation_status": self.investigation_status()})

    def context(self):
        items = sorted(self.candidates.values(), key=lambda item: (item["state"] == "closed", -item["step"]))
        pipeline = pipeline_view(list(self.discovery_summaries.values()), self.executions, self.rejected_actions)
        discovery = {"verification": "provided_text_declarations_only", "omitted_sources": self.omitted_discovery,
                     "truncated": any(item["truncated"] for item in self.discovery_summaries.values())}
        for key in ("assets", "surfaces", "hints"):
            entries = []
            seen = set()
            for summary in reversed(list(self.discovery_summaries.values())):
                for entry in summary[key]:
                    identity = json.dumps({k: v for k, v in entry.items() if k != "step"}, sort_keys=True)
                    if identity not in seen:
                        seen.add(identity)
                        entries.append({**entry, "source_url": summary["source_url"]})
            discovery[key] = entries[:24]
            discovery["omitted_" + key] = max(0, len(entries) - 24)
        return copy.deepcopy({"candidates": items[:8], "omitted_candidates": max(0, len(items) - 8),
            "finish_review": pipeline["review"], "pending_resources": pipeline["pending_resources"],
            "omitted_pending": pipeline["omitted_pending"], "recovery": pipeline["failures"][-4:],
            "omitted_recovery": max(0, len(pipeline["failures"]) - 4) + pipeline["omitted_failures"],
            "discovery": discovery,
            "executions": self.executions[-4:], "omitted_executions": max(0, len(self.executions) - 4),
            "rejected_actions": self.rejected_actions[-2:], "omitted_rejections": max(0, len(self.rejected_actions) - 2),
            "omitted_notes": self.omitted_notes,
            "memory_path": MEMORY_PATH, "advisories": self.advisories,
            "instruction": "Optional model-authored notes, not a checklist or verified findings. Discovery holds untrusted declarations, not confirmed endpoints or completed analysis. Choose any useful in-scope next step. Read memory_path for retained notes and discovery summaries. Closed means work stopped, never that the site is safe."})


def pack_j2_context(context, budget):
    from j1_policy import pack_j1_context
    result = copy.deepcopy(context)
    while True:
        try:
            return pack_j1_context(result, budget)
        except ValueError as exc:
            memory = result.get("j2", {})
            trimmed = False
            for key, counter in (("executions", "omitted_executions"), ("rejected_actions", "omitted_rejections")):
                if len(memory.get(key, [])) > 1:
                    memory[key].pop(0)
                    memory[counter] = memory.get(counter, 0) + 1
                    trimmed = True
                    break
            if trimmed:
                memory["truncated"] = True
                continue
            records = memory.get("executions", []) + [item.get("result", {}) for item in result.get("recent", [])]
            for record in records:
                for key in ("http_observations", "helper_errors", "helper_outcomes", "blocked_requests"):
                    if isinstance(record.get(key), list) and record[key]:
                        record[key].pop(0)
                        record["omitted_" + key] = record.get("omitted_" + key, 0) + 1
                        trimmed = True
                        break
                if trimmed:
                    break
            if trimmed:
                memory["truncated"] = True
                continue
            for key, counter in (("pending_resources", "omitted_pending"), ("recovery", "omitted_recovery")):
                if memory.get(key):
                    memory[key].pop()
                    memory[counter] = memory.get(counter, 0) + 1
                    trimmed = True
                    break
            if trimmed:
                memory["truncated"] = True
                continue
            discovery = memory.get("discovery", {})
            largest = max(("assets", "surfaces", "hints"), key=lambda key: len(discovery.get(key, [])))
            if discovery.get(largest):
                discovery[largest].pop()
                discovery["omitted_" + largest] = discovery.get("omitted_" + largest, 0) + 1
                discovery["truncated"] = True
                continue
            cards = memory.get("candidates", [])
            if len(cards) <= 1:
                changed = False
                for item in cards:
                    for key in ("basis", "next_observation"):
                        if len(item.get(key, "")) > 160:
                            item[key] = item[key][:120] + " [full value in memory_path]"
                            changed = True
                    if item.get("evidence_paths"):
                        item["evidence_paths"] = []
                        changed = True
                if changed:
                    memory["truncated"] = True
                    continue
                for key in ("progress_limits", "instruction"):
                    if key in memory:
                        del memory[key]
                        changed = True
                if changed:
                    memory["truncated"] = True
                    continue
                raise ValueError("J2 controls and goal exceed the input budget; shorten the goal or increase num_ctx") from exc
            cards.pop()
            memory["omitted_candidates"] = memory.get("omitted_candidates", 0) + 1
            memory["truncated"] = True

"""J1 workflow bookkeeping. Stages and retained code are not independent HTTP evidence."""

from __future__ import annotations

import ast
import copy
import hashlib
import json
import re
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit, urlunsplit


CHECKS = ("SQLi", "IDOR", "XSS", "OTHER")
STAGES = ("baseline", "compare", "verify")
_PLAN = re.compile(r"^(RECON|SQLi|IDOR|XSS|OTHER|REPORT)/(discover|baseline|compare|verify|blocked|report):\s*\S")


def _progress(check):
    return {"check": check, "state": "not_started", "stages": [], "stage_steps": {}, "steps": [],
            "basis": "", "basis_steps": [], "limits": "Applicability not assessed; no test has been run."}


def pack_j1_context(context, budget):
    """Keep control/source references intact; shrink previews and optional request descriptions."""
    def preview(value, limit, key=""):
        if isinstance(value, str) and key in {"plan", "reason", "claim", "observed", "error", "stdout", "stderr", "exception", "command", "code", "content"} and len(value) > limit:
            return value[:limit // 2] + "\n[preview; original in log]\n" + value[-limit // 2:]
        if isinstance(value, dict):
            return {name: preview(child, limit, name) for name, child in value.items()}
        if isinstance(value, list):
            return [preview(child, limit, key) for child in value]
        return value
    for limit in (1000, 400, 120, 40):
        result = {**context, **{key: preview(context[key], limit) for key in ("history", "recent") if key in context}}
        while len(json.dumps(result, ensure_ascii=False)) > budget and len(result.get("history", [])) > 1:
            result["history"].pop(0)
        if len(json.dumps(result, ensure_ascii=False)) <= budget:
            return result
    while len(json.dumps(result, ensure_ascii=False)) > budget and len(result.get("recent", [])) > 1:
        result["recent"].pop(0)
    result["history"] = []
    if len(json.dumps(result, ensure_ascii=False)) > budget and "j1" in result:
        result["j1"] = copy.deepcopy(result["j1"])
        for item in result["j1"].get("request_memory", []):
            item.pop("endpoint", None)
            item.pop("form_fields", None)
    if len(json.dumps(result, ensure_ascii=False)) > budget:
        raise ValueError("J1 controls and goal exceed the input budget; shorten the goal or increase num_ctx")
    return result


class J1Policy:
    def __init__(self, target):
        self.target = urlsplit(target)
        self.coverage = {check: _progress(check) for check in CHECKS}
        self.candidates = {}
        self.request_memory = []
        self.observations = {}
        self.focus_check = None
        self.unlinked_steps = []
        self.advisories = []
        self.pending_issue = None

    def allow_action(self, command, report_only):
        """Keep bookkeeping errors visible without spending a model call on format repair."""
        if report_only and command.action != "finish":
            raise ValueError("Report reserve is active: return action finish; do not execute more tools")
        self.pending_issue = None
        if command.action != "finish":
            try:
                self.validate(command, False)
            except ValueError as exc:
                self.pending_issue = str(exc)

    @staticmethod
    def labels(command):
        match = _PLAN.match(command.plan)
        if not match:
            raise ValueError("Progress label not recognized; use CHECK/STAGE: to link this step to a candidate")
        return match.group(1), match.group(2)

    @staticmethod
    def references(command):
        return {int(value) for value in re.findall(r"\[step:(\d{1,6})\]", command.reason)}

    def candidate(self, check, refs):
        matches = [item for step, item in self.candidates.items() if step in refs and item["check"] == check]
        if len(matches) != 1:
            raise ValueError(f"To link {check} comparison/verification, cite exactly one candidate's baseline [step:N]")
        return matches[0]

    def validate(self, command, report_only):
        check, stage = self.labels(command)
        if command.action == "finish":
            if (check, stage) != ("REPORT", "report"):
                raise ValueError("finish needs plan REPORT/report: ... and a complete report in reason")
            return
        if report_only:
            raise ValueError("Report reserve is active: return action finish with plan REPORT/report: ...; do not execute more tools")
        if check == "REPORT" or stage == "report" or (check == "RECON" and stage not in {"discover", "blocked"}):
            raise ValueError("Use RECON/discover for observations, CHECK/STAGE for a relevant candidate, or REPORT/report with finish")
        if check in CHECKS and stage in STAGES:
            refs = self.references(command)
            if not refs or any(ref not in self.observations for ref in refs):
                raise ValueError("To link this stage, cite an existing nonempty observation in reason, e.g. [step:3]")
            previous = STAGES[:STAGES.index(stage)]
            row = self.candidate(check, refs) if previous else None
            if previous and any(item not in row["stages"] for item in previous):
                raise ValueError(f"To link {check}/{stage}, record prior stages {', '.join(previous)}")
            required = {row["stage_steps"][item] for item in previous}
            if not required.issubset(refs):
                raise ValueError(f"To link {check}/{stage}, cite its current baseline/comparison steps in reason: "
                                 + " ".join(f"[step:{step}]" for step in sorted(required)))
        if stage in {"compare", "verify"}:
            shapes = self._request_shapes(command.code)
            baseline_shapes = [item for item in self.request_memory if item["step"] == row["baseline_step"]
                               and item["endpoint"] != "unresolved"]
            if len(baseline_shapes) == len(shapes) == 1 and (
                    baseline_shapes[0]["endpoint"], baseline_shapes[0]["method"]) != (
                    shapes[0]["endpoint"], shapes[0]["method"]):
                raise ValueError("Request endpoint or method changed; confirm an intentional change or start a new candidate")
            for shape in shapes:
                if shape["form_fields"] is None:
                    continue
                saved = [item for item in self.request_memory if item["step"] == row["baseline_step"]
                         and item["form_fields"] is not None
                         and (item["endpoint"], item["method"]) == (shape["endpoint"], shape["method"])]
                if saved and not any(item["form_fields"] == shape["form_fields"] for item in saved):
                    names = ", ".join(saved[0]["form_fields"])
                    raise ValueError(f"Request fields changed from [{names}] in {saved[0]['source_path']}; confirm an intentional change or start a new candidate")

    def _request_shapes(self, code):
        # shortcut: static named HTTP calls only; add runtime request capture if dynamic schemas must be enforced.
        try:
            tree = ast.parse(code)
        except SyntaxError:
            return []
        bindings = {target.id: node.value for node in ast.walk(tree) if isinstance(node, ast.Assign)
                    for target in node.targets if isinstance(target, ast.Name)}
        receivers = {"requests", "httpx", "httpx2", "page.request", "context.request"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                receivers.update(alias.asname or alias.name for alias in node.names if alias.name in {"requests", "httpx", "httpx2"})
        for name, value in bindings.items():
            if isinstance(value, ast.Call) and ast.unparse(value.func) in {
                    f"{receiver}.{constructor}" for receiver in receivers for constructor in ("Session", "Client", "AsyncClient")}:
                receivers.add(name)
        bindings.setdefault("target_origin", ast.Constant(f"{self.target.scheme}://{self.target.netloc}"))
        def string(node, depth=0):
            if depth > 4:
                return None
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                return node.value
            if isinstance(node, ast.Name) and node.id in bindings:
                return string(bindings[node.id], depth + 1)
            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
                left, right = string(node.left, depth + 1), string(node.right, depth + 1)
                return left + right if left is not None and right is not None else None
            if isinstance(node, ast.JoinedStr):
                if any(isinstance(part, ast.FormattedValue) and (part.conversion != -1 or part.format_spec is not None) for part in node.values):
                    return None
                parts = [string(part.value if isinstance(part, ast.FormattedValue) else part, depth + 1) for part in node.values]
                return "".join(parts) if all(part is not None for part in parts) else None
            return None
        shapes = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            method = node.func.attr.lower() if isinstance(node.func, ast.Attribute) else ""
            if isinstance(node.func, ast.Attribute) and ast.unparse(node.func.value) not in receivers:
                continue
            if isinstance(node.func, ast.Name) and node.func.id in {"http_get", "get_text", "get_json"}:
                method = "get"
            url_index = 0
            if method == "request":
                method = string(node.args[0]) if node.args else next((string(k.value) for k in node.keywords if k.arg == "method"), None)
                method = (method or "").lower()
                url_index = 1
            if method not in {"get", "post", "put", "patch", "delete", "head", "options"}:
                continue
            url = string(node.args[url_index]) if len(node.args) > url_index else next((string(k.value) for k in node.keywords if k.arg == "url"), None)
            if url is not None and len(url) > 2048:
                continue
            try:
                parts = urlsplit(url or "")
                if ((parts.scheme, parts.hostname, parts.port) != (self.target.scheme, self.target.hostname, self.target.port)
                        or parts.username or parts.password):
                    continue
            except ValueError:
                continue
            try:
                fields = {key for key, _ in parse_qsl(parts.query, keep_blank_values=True, max_num_fields=64)}
            except ValueError:
                continue
            unknown_fields = False
            for keyword in node.keywords:
                if keyword.arg is None:
                    unknown_fields = True
                if keyword.arg not in {"files", "data", "params", "form", "json"}:
                    continue
                value = bindings.get(keyword.value.id) if isinstance(keyword.value, ast.Name) else keyword.value
                if isinstance(value, ast.Dict):
                    fields.update(key.value for key in value.keys if isinstance(key, ast.Constant) and isinstance(key.value, str))
                    unknown_fields |= any(not isinstance(key, ast.Constant) or not isinstance(key.value, str) for key in value.keys)
                elif not isinstance(value, ast.Constant) or value.value is not None:
                    unknown_fields = True
            shapes.append({"endpoint": urlunsplit((parts.scheme, parts.netloc, parts.path, "", "")),
                           "method": method.upper(), "form_fields": sorted(fields) if not unknown_fields and len(fields) <= 24 else None})
        return shapes

    def record(self, command, execution, step):
        if self.pending_issue:
            observed = execution.stdout.strip()
            if (execution.exit_code == 0 and not execution.timed_out and not execution.cancelled and observed
                    and command.action in {"run_python", "run_command", "read_file"}):
                self.observations[step] = {"step": step, "check": "UNLINKED", "stage": "",
                                           "excerpt": " ".join(observed.split())[:160]}
            self.unlinked_steps.append(step)
            self.advisories.append(f"Step {step}: {self.pending_issue}")
            self.unlinked_steps = self.unlinked_steps[-12:]
            self.advisories = self.advisories[-4:]
            self.pending_issue = None
            return
        check, stage = self.labels(command)
        ok = execution.exit_code == 0 and not execution.timed_out and not execution.cancelled
        observed = execution.stdout.strip()
        reported_status = None
        try:
            output = json.loads(observed)
            if isinstance(output, dict):
                reported_status = output.get("status", output.get("status_code"))
        except (ValueError, TypeError):
            pass
        if stage == "baseline" and type(reported_status) is int and reported_status >= 400:
            ok = False
        if ok and observed and stage != "blocked" and command.action in {"run_python", "run_command", "read_file"}:
            # shortcut: output references establish traceability, not HTTP authenticity or semantic relevance.
            self.observations[step] = {"step": step, "check": check, "stage": stage,
                                       "excerpt": " ".join(observed.split())[:160]}
        if check in CHECKS:
            self.focus_check = check
            row = self.candidate(check, self.references(command)) if stage in {"compare", "verify"} else _progress(check)
            if stage == "blocked":
                matches = [item for key, item in self.candidates.items() if key in self.references(command) and item["check"] == check]
                if len(matches) == 1:
                    row = matches[0]
            if stage == "baseline":
                row["baseline_step"] = step
                self.candidates[step] = row
            row["steps"] = (row["steps"] + [step])[-12:]
            row["state"] = "in_progress"
            if stage in STAGES:
                previous = STAGES[:STAGES.index(stage)]
                row["stages"] = [item for item in row["stages"] if item in previous]
                row["stage_steps"] = {item: value for item, value in row["stage_steps"].items() if item in previous}
                row["limits"] = f"No usable result recorded yet for {stage} at step {step}; security outcome remains unverified."
            if stage == "baseline":
                row["basis"] = command.reason[:320]
                row["basis_steps"] = sorted(self.references(command))
            if stage == "blocked" and ok:
                row["state"], row["limits"] = "blocked", command.reason[:240]
            elif not ok:
                row["limits"] = f"Execution failed at step {step}; prerequisites and security outcome remain unverified."
            elif ok and observed and stage in STAGES:
                if stage not in row["stages"]:
                    row["stages"].append(stage)
                row["stage_steps"][stage] = step
                row["limits"] = "Stage execution recorded; input processing, controls and security impact are not independently verified."
                if all(item in row["stages"] for item in STAGES):
                    row["state"] = "reviewed_unverified"
            elif stage == "discover":
                row["limits"] = "Discovery only; applicable input/object/display context and test prerequisites still need assessment."
            self.coverage[check] = copy.deepcopy(row)
        if ok and observed and stage == "baseline" and command.action == "run_python" and execution.path:
            shapes = self._request_shapes(command.code) or [{"endpoint": "unresolved", "method": "unknown", "form_fields": None}]
            for shape in shapes:
                if any(item["step"] == step and all(item[k] == shape[k] for k in shape) for item in self.request_memory):
                    continue
                path = Path(execution.path)
                source_path = (Path("tools") / path.name).as_posix() if path.is_absolute() else path.as_posix()
                self.request_memory.append({**shape, "check": check, "step": step, "source_path": source_path,
                    "source_sha256": hashlib.sha256(command.code.encode()).hexdigest(), "artifact_path": execution.artifact_path,
                    "verification": "execution_only; HTTP success and vulnerability not independently verified"})
                row["source_path"] = source_path
            self.request_memory = self.request_memory[-8:]

    def context(self):
        # The full shapes stay in metadata; keep every source reference even if optional details do not fit.
        items = sorted(reversed(self.request_memory), key=lambda item: item["check"] != self.focus_check)
        cards = [{key: item[key] for key in ("check", "step", "source_path", "source_sha256", "method")} for item in items]
        for card, item in zip(cards, items):
            for key in ("form_fields", "endpoint"):
                card[key] = item[key]
                if len(json.dumps(cards, ensure_ascii=False)) > 2800:
                    del card[key]
        coverage = {check: {key: (value[:160] + "…" if key == "basis" and len(value) > 160 else value)
                            for key, value in row.items() if key not in {"check", "source_path"}}
                    for check, row in self.coverage.items()}
        return copy.deepcopy({"coverage": coverage, "request_memory": cards,
            "candidate_refs": [{key: item[key] for key in ("baseline_step", "check", "state", "stage_steps", "source_path") if key in item}
                               for item in list(self.candidates.values())[-4:]],
            "observation_refs": list(self.observations.values())[-8:],
            "unlinked_steps": self.unlinked_steps, "advisories": self.advisories,
            "open_checks": [check for check, row in self.coverage.items() if row["state"] == "in_progress"],
            "instruction": "Choose the best supported in-scope next step. CHECK/STAGE labels and [step:N] references help track a candidate but are optional; unlinked steps still execute and appear in advisories. No fixed attack order or required coverage. Output references do not prove applicability or vulnerability."})

    def snapshot(self):
        return copy.deepcopy({"coverage": self.coverage, "candidates": self.candidates, "request_memory": self.request_memory,
                              "unlinked_steps": self.unlinked_steps, "advisories": self.advisories})

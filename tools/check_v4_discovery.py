"""Explicit, recorded HTTP-only discovery validation; dry-run unless --execute."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import subprocess
import threading
import time
import types
import uuid
from pathlib import Path
from engine.paths import PROJECT_ROOT
from urllib.parse import urlsplit

from engine.command_runtime import CommandRuntime
import engine.harness as harness_module
from orchestrator import RUNS_ROOT, context_char_budget, safe_target, validate_budgets
from engine.experiment_config import validate_experiment_config
from engine.lab import TARGET as LAB_TARGET, reset_lab
from engine.model_settings import validate_model_settings
from engine.planning import load_prompt
from engine.planning import COMMAND_SCHEMA
from engine.session_log import ControllerLease, SessionStore
from engine.policy import execution_settings
from engine.reporting import finalize_v4
from engine.tree import TreeStore
from tools._legacy_imports import baseline_builtins


ROOT = PROJECT_ROOT
REFERENCE_RUN = "c9e2de5fd82d4057a63fb42147390b2d"
HTTP_ONLY = """HTTP-only validation environment override: no browser is launched.
For target observations use action=run_python with synchronous Python code in
the code field. This validation executes code as a local Python process in
runtime_path. It does not supply page, context, browser, or browser helpers.
Use requests or urllib for same-origin HTTP requests. Write the required result
JSON to attempt.result_path and preserve response files under work/ as evidence.
Do not import Playwright, Selenium, webbrowser, pyautogui, or launch a browser.
For a normal investigation decision, use run_python, read_file, or finish.
write_file is reserved for the harness's explicit result-repair instruction.
Every Command needs nonempty plan and reason. For run_python, set path, content,
and command to empty strings. Do not use top-level await or browser APIs.
Keep the result and evidence contracts supplied above unchanged.
"""
_PRINT_LOCK = threading.Lock()


def emit(value):
    with _PRINT_LOCK:
        print(json.dumps(value, ensure_ascii=False), flush=True)


def git_bytes(*args):
    return subprocess.run(["git", *args], cwd=ROOT, check=True, capture_output=True).stdout


def sha(value):
    return hashlib.sha256(value).hexdigest()


def source_info(baseline_ref):
    commit = git_bytes("rev-parse", "HEAD").decode().strip()
    diff = git_bytes("diff", "--binary", "HEAD", "--", ".")
    untracked = {}
    for name in git_bytes("ls-files", "--others", "--exclude-standard").decode("utf-8").splitlines():
        path = ROOT / name
        if path.suffix == ".py" and path.is_file() and path.resolve().is_relative_to(ROOT):
            untracked[name] = sha(path.read_bytes())
    if baseline_ref:
        resolved = git_bytes("rev-parse", "--verify", baseline_ref + "^{commit}").decode().strip()
        try:
            loop = git_bytes("show", resolved + ":v4_loop.py")
        except subprocess.CalledProcessError:
            try:
                loop = git_bytes("show", resolved + ":strategies/greedy.py")
            except subprocess.CalledProcessError as exc:
                raise ValueError("Baseline commit has no supported loop source") from exc
        prompt = git_bytes("show", resolved + ":prompts/system/v4.txt")
    else:
        resolved = None
        loop = (ROOT / "strategies" / "greedy.py").read_bytes()
        prompt = (ROOT / "prompts/system/v4.txt").read_bytes()
    return {"commit": commit, "diff_sha256": sha(diff), "untracked_python_sha256": untracked,
            "baseline_ref": resolved, "loop_sha256": sha(loop), "base_prompt_sha256": sha(prompt)}, loop, prompt.decode("utf-8")


def runner_type(source, baseline_ref, prompt):
    if not baseline_ref:
        from strategies.greedy import V4Runner
        return V4Runner
    module = types.ModuleType("discovery_baseline_" + baseline_ref[:12])
    module.__file__ = f"git:{baseline_ref}:v4_loop.py"
    module.__dict__["__builtins__"] = baseline_builtins()
    exec(compile(source, module.__file__, "exec"), module.__dict__)
    base_digest = sha(prompt.encode("utf-8"))
    module.load_prompt = lambda version: (prompt, base_digest) if version == "v4" else load_prompt(version)
    return module.V4Runner


def configure(args):
    reference = SessionStore(RUNS_ROOT).read(args.reference_run)
    experiment = validate_experiment_config({key: reference.get(key) for key in (
        "experiment_mode", "scorer_provider", "scorer_model", "scorer_settings")})
    if experiment["experiment_mode"] != "v4":
        raise ValueError("Reference must be a v4 run")
    budgets = validate_budgets(reference.get("budgets"))
    budgets["model_calls"] = args.model_calls if args.model_calls is not None else min(40, budgets["model_calls"])
    if args.seconds is not None:
        budgets["seconds"] = args.seconds
    budgets = validate_budgets(budgets)
    limits = execution_settings(reference.get("v4_settings"))
    if args.node_attempts is not None:
        limits["node_attempts"] = args.node_attempts
    limits["max_cost_usd"] = args.max_cost_usd if args.max_cost_usd is not None else min(.10, limits["max_cost_usd"])
    limits = execution_settings(limits)
    target = safe_target(reference["target"])
    return {**experiment, "target": target, "goal": args.goal or reference["goal"],
            "model": reference["model"], "model_settings": validate_model_settings(reference["model_settings"]),
            "budgets": budgets, "v4_settings": limits, "prompt_version": "v4", "v4_stage": "execution",
            "execution_ready": True, "validation_profile": "http-only-v1", "profile_name": args.profile_name,
            "reference_run_id": args.reference_run}


def summary(log, result, reason):
    report_path = log.run_dir / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8")) if report_path.exists() else {}
    stats, usage = report.get("stats") or {}, report.get("usage") or {}
    return {"run_id": log.run_id, "result": report.get("run_result", result),
            "stop_reason": report.get("stop_reason", reason), "surfaces": stats.get("surfaces"),
            "candidates": stats.get("candidates"), "tested_candidates": stats.get("tested_candidates"),
            "incomplete_nodes": report.get("incomplete_nodes"), "cost_usd": usage.get("cost_usd")}


def execute(config, provenance, source, base_prompt):
    lease = ControllerLease(RUNS_ROOT)
    if not lease.acquire():
        raise RuntimeError("Another controller owns the local lab")
    log = runtime = None
    done = threading.Event()
    progress = {"result": "running", "run_id": None, "model_calls": 0, "tool_calls": 0}
    progress_lock = threading.Lock()
    heartbeat = None
    result, reason = "error", "validation_setup_failed"
    try:
        store = SessionStore(RUNS_ROOT)
        ready, blocked = store.can_start()
        if not ready:
            raise RuntimeError(blocked)
        run_id = uuid.uuid4().hex
        owner = uuid.uuid4().hex
        prompt = base_prompt + "\n" + HTTP_ONLY
        metadata = {**config, "owner_instance_id": owner, "controller_pid": os.getpid(),
            "started_at": time.time(), "finished_at": None, "managed_processes": [],
            "runtime_path": str(RUNS_ROOT / run_id / "runtime"), "runtime_creation_intent": False,
            "initial_lab_status": "pending" if config["target"] == LAB_TARGET else "not_applicable",
            "lab_reset_status": "pending" if config["target"] == LAB_TARGET else "not_applicable",
            "validation_source": provenance, "validation_prompt_sha256": sha(prompt.encode("utf-8")),
            "validation_environment_sha256": sha(HTTP_ONLY.encode("utf-8")),
            "validation_http_executor": "run_python_as_local_python_process"}
        log = store.create(run_id, metadata)
        runtime = CommandRuntime(RUNS_ROOT, log)
        progress["run_id"] = run_id

        def listener(event):
            kind = event["kind"]
            if kind not in ("model_request", "exec_finished", "node_result_imported", "node_investigation_finished", "scorer_request_started"):
                return
            with progress_lock:
                if kind in ("model_request", "scorer_request_started"):
                    progress["model_calls"] += 1
                if kind == "exec_finished":
                    progress["tool_calls"] += 1
                progress.update(last_event=kind, seq=event["seq"])
                emit(dict(progress))

        def heartbeat_loop():
            while not done.wait(20):
                with progress_lock:
                    emit(dict(progress))

        log.listen(listener)
        heartbeat = threading.Thread(target=heartbeat_loop, daemon=True)
        heartbeat.start()
        log.append("run_created", {"owner_instance_id": owner, "goal": config["goal"]})
        log.append("v4_execution_created", {"configuration": metadata})
        TreeStore(log).initialize(config["target"])
        if config["target"] == LAB_TARGET:
            log.update(initial_lab_status="running")
            probe = reset_lab()
            if probe.get("verified") is not True:
                raise RuntimeError("Initial lab reset failed")
            log.append("initial_lab_reset", {"probe": probe})
            log.update(initial_lab_status="succeeded", initial_lab_probe=probe)
        log.update(phase="running")
        runtime.start_session(run_id, target=config["target"], browser=False)
        runner = runner_type(source, provenance["baseline_ref"], base_prompt)(log, runtime)
        runner.harness.system_prompt = prompt
        runner.harness.system_prompt_sha256 = sha(prompt.encode("utf-8"))
        runner.context_budget = context_char_budget(config["model_settings"]["options"].get("num_ctx", 8192), prompt)
        original_decide = runner.harness.decide

        def http_only_decide(run_id, context, **kwargs):
            allowed = (["write_file"] if context.get("instruction", "").startswith("The preceding tool already ran")
                       else ["run_python", "read_file", "finish"])
            schema = copy.deepcopy(COMMAND_SCHEMA)
            schema["properties"]["action"]["enum"] = allowed
            schema["oneOf"] = [branch for branch in schema["oneOf"]
                               if branch["properties"]["action"]["const"] in allowed]
            harness_module.COMMAND_SCHEMA = schema
            return original_decide(run_id, context, **kwargs)

        runner.harness.decide = http_only_decide
        original_validator = runner._validate_command

        target_parts = urlsplit(config["target"])
        target_origin = (target_parts.scheme, target_parts.hostname,
                         target_parts.port or (443 if target_parts.scheme == "https" else 80))

        def http_only_validator(command, result_path, **kwargs):
            text = (command.code if command.action == "run_python" else
                    command.content if command.action == "write_file" else command.command)
            if re.search(r"\b(playwright|selenium|pyppeteer|webbrowser|pyautogui|chromium|msedge)\b", text, re.I):
                raise ValueError("Browser and UI automation are unavailable in this HTTP-only validation")
            for address in re.findall(r"https?://[^\s\"'`<>]+", text):
                parts = urlsplit(address.rstrip("),;"))
                origin = (parts.scheme, parts.hostname, parts.port or (443 if parts.scheme == "https" else 80))
                if origin != target_origin:
                    raise ValueError("HTTP-only validation permits target-origin URLs only")
            if command.action == "run_python":
                if result_path not in command.code:
                    raise ValueError("run_python must write result JSON to attempt.result_path before it exits")
                try:
                    compile(command.code, "<validation-http-code>", "exec")
                except SyntaxError as exc:
                    raise ValueError(f"Python syntax error on line {exc.lineno}: {exc.msg}") from exc
                return command
            return original_validator(command, result_path, **kwargs)

        runner._validate_command = http_only_validator
        original_run = runtime.run

        def http_only_run(run_id, command, *, call_id="", timeout=30, on_output=None):
            if command.get("action") != "run_python":
                return original_run(run_id, command, call_id=call_id, timeout=timeout, on_output=on_output)
            code = command["code"]
            script = f"work/http-only/{call_id}.py"
            path = runtime._file_path(script, writing=True)
            path.write_text(code, encoding="utf-8")
            log.append("validation_http_python_saved", {"call_id": call_id, "path": script,
                "sha256": sha(code.encode("utf-8"))})
            shell_command = f'"{runtime.python}" "{script}"'
            return original_run(run_id, {"action": "run_command", "command": shell_command},
                                call_id=call_id, timeout=timeout, on_output=on_output)

        runtime.run = http_only_run
        result, reason = runner.run()
    except BaseException as exc:
        result = "interrupted" if isinstance(exc, KeyboardInterrupt) else "error"
        reason = "validation_" + type(exc).__name__.lower()
        if log is None:
            raise
        log.update(validation_exception_type=type(exc).__name__)
    finally:
        try:
            if log is not None and runtime is not None:
                finalize_v4(log, runtime, result, reason, lab_reset=reset_lab)
        finally:
            done.set()
            if heartbeat is not None:
                heartbeat.join(timeout=2)
            lease.release()
    final = summary(log, result, reason)
    emit(final)
    return 0 if final["result"] == "completed" else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--reference-run", default=REFERENCE_RUN)
    parser.add_argument("--profile-name", default="discovery-baseline")
    parser.add_argument("--baseline-ref", help="Load the execution loop and prompt from this Git commit")
    parser.add_argument("--goal")
    parser.add_argument("--model-calls", type=int)
    parser.add_argument("--node-attempts", type=int)
    parser.add_argument("--max-cost-usd", type=float)
    parser.add_argument("--seconds", type=int)
    args = parser.parse_args()
    config = configure(args)
    provenance, source, prompt = source_info(args.baseline_ref)
    if not args.execute:
        emit({"run_id": None, "result": "dry_run", "stop_reason": "explicit_execute_required",
              "surfaces": None, "candidates": None, "tested_candidates": None, "incomplete_nodes": None,
              "cost_usd": 0})
        return 0
    return execute(config, provenance, source, prompt)


if __name__ == "__main__":
    raise SystemExit(main())

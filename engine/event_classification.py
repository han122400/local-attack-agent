"""Event semantics shared by the append-only sink and read-only legacy views.

Do not infer failures from output text (including HTTP status codes). retryable
means another attempt may be useful, often after revision; it never triggers one.
"""


def classify_event(kind: str, values: dict, *, legacy: bool = False) -> dict:
    category, component, outcome = "lifecycle", "session", "recorded"
    cause, retryable, source = None, None, None
    if kind.startswith("model_") or kind in {"command_decoded", "command_normalized"}:
        category, component = "model", "ollama"
    elif kind in {"exec_started", "exec_finished", "file_changed"}:
        category, component = "execution", "runtime"
    elif kind in {"exec_output", "observation", "http_response"}:
        category, component, outcome = "observation", "runtime", "observed"
    elif kind.startswith(("runtime_", "process_", "browser_")) or kind == "worker_output":
        category, component = "system", "browser_worker" if kind.startswith("browser_") or kind == "worker_output" else "runtime"
    if kind in {"model_request", "exec_started"}:
        outcome = "started"
    elif kind in {"command_decoded", "model_response"}:
        outcome = "received"
    elif kind == "exec_finished":
        source = values.get("failure_source")
        component = {"run_python": "browser_worker", "run_command": "cmd",
                     "read_file": "file_api", "write_file": "file_api"}.get(values.get("action"), "runtime")
        if values.get("cancelled"):
            outcome, cause, retryable, source = "cancelled", "execution_cancelled", False, "control"
        elif source == "system":
            category, outcome, cause, retryable = "system", "system_error", "executor_failure", True
        elif values.get("timed_out"):
            outcome, cause, retryable, source = "timed_out", "execution_timeout", True, "execution"
        elif values.get("exit_code") == 0:
            outcome = "succeeded"
        else:
            outcome, retryable = "failed", True
            source = source or "unknown"
            cause = "legacy_execution_failure" if legacy else "execution_failed"
    elif kind == "model_error":
        error = values.get("error_kind", "unknown")
        source, cause, outcome, retryable = "model", "model_" + error, "failed", True
        if error == "transport":
            category, source, cause = "system", "system", "model_transport_failure"
        elif error == "cancelled":
            category, source, outcome, retryable = "lifecycle", "control", "cancelled", False
        elif error == "budget":
            category, source, outcome, retryable = "lifecycle", "control", "exhausted", False
        elif error == "timed_out":
            outcome = "timed_out"
    elif kind in {"run_error", "finalize_failed"}:
        category, source, outcome, retryable = "system", "system", "failed", True
        cause = "executor_failure" if kind == "run_error" else "finalization_failed"
    elif kind in {"lab_reset", "initial_lab_reset", "cleanup_step", "run_finalized"}:
        outcome = values.get("status", "succeeded")
        if kind in {"lab_reset", "initial_lab_reset"}:
            component = "lab"
        # Successful cleanup is independent of the investigation's run_result.
    fields = {"event_schema": 1, "category": category, "component": component,
              "outcome": outcome, "cause_code": cause, "retryable": retryable,
              "failure_source": source, "call_id": None, "exec_id": None,
              "message": values.get("exception") or values.get("error") or values.get("message") or ""}
    for key in fields:
        if key in values and values[key] is not None:
            fields[key] = values[key]
    return fields


def event_view(event: dict) -> dict:
    """Enrich an old record in memory; never rewrite its stored bytes."""
    if event.get("event_schema") == 1:
        return event
    return {**event, **classify_event(event["kind"], event, legacy=True)}
